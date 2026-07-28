"""EncryptedCharField round-trips, feed content validation, cache filename
derivation, orphaned-file cleanup and the production secret-key guard."""
import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.base import ContentFile
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone

from OtwarteDaneTransportowe.settings_base import validate_production_secret_key
from cases.models import TransportOrganization
from data_manager.models import (
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    FeedValidationReport,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)
from data_manager.scheduler import _fetch_realtime_endpoint_rt, _fetch_static_entry
from data_manager.validators import (
    InvalidFeedContent,
    looks_like_zip,
    validate_realtime_feed_content,
    validate_static_feed_content,
    validate_uploaded_gtfs_zip,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def provider_user():
    user = get_user_model().objects.create_user('fc-provider', 'fc@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='DataProvider')
    user.groups.add(group)
    return user


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _entry(org, user, **kwargs):
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=user, data_type='gtfs', name='F',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission, event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3, stage_after=4, actor=user,
    )
    defaults = {
        'url': 'https://example.org/gtfs.zip',
        'hide_original': True,
        'download_time_1': '03:00',
    }
    defaults.update(kwargs)
    return StaticFeedEntry.objects.create(submission=submission, **defaults)


class _Response:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


# ---------------------------------------------------------------------------
# EncryptedCharField
# ---------------------------------------------------------------------------

def test_encrypted_field_round_trip_with_key(settings, org, provider_user):
    settings.FEED_AUTH_ENCRYPTION_KEY = Fernet.generate_key().decode()
    entry = _entry(org, provider_user, auth_type='api_key', auth_value='top-secret')

    raw = StaticFeedEntry.objects.filter(pk=entry.pk).values_list('auth_value', flat=True)[0]
    # values_list still decrypts via from_db_value; check the prep value instead.
    stored = StaticFeedEntry._meta.get_field('auth_value').get_prep_value('top-secret')
    assert stored.startswith('fernet$')
    assert 'top-secret' not in stored

    entry.refresh_from_db()
    assert entry.auth_value == 'top-secret'
    assert raw == 'top-secret'  # application always sees plaintext


def test_encrypted_field_reads_legacy_plaintext(settings, org, provider_user):
    settings.FEED_AUTH_ENCRYPTION_KEY = Fernet.generate_key().decode()
    entry = _entry(org, provider_user, auth_type='api_key', auth_value='x')
    # Simulate a pre-encryption row written before the key was configured.
    StaticFeedEntry.objects.filter(pk=entry.pk).update(auth_value='legacy-plain')
    entry.refresh_from_db()
    assert entry.auth_value == 'legacy-plain'


def test_encrypted_field_without_key_stores_plaintext(settings, org, provider_user):
    settings.FEED_AUTH_ENCRYPTION_KEY = None
    field = StaticFeedEntry._meta.get_field('auth_value')
    assert field.get_prep_value('dev-secret') == 'dev-secret'


# ---------------------------------------------------------------------------
# S2 regression: report file saved before the report is linked to its entry
# ---------------------------------------------------------------------------

def test_unlinked_validation_report_file_saves_under_unknown(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    report = FeedValidationReport.objects.create(error_count=0, warning_count=0)
    # Previously crashed with NameError (ObjectDoesNotExist not imported).
    report.report_file.save('report.json', ContentFile(b'{}'), save=True)
    assert report.report_file.name.startswith('unknown/validation/')


# ---------------------------------------------------------------------------
# Content validation (unit)
# ---------------------------------------------------------------------------

def test_looks_like_zip():
    assert looks_like_zip(b'PK\x03\x04rest')
    assert not looks_like_zip(b'<!doctype html>')


@pytest.mark.parametrize('data_type,content,ok', [
    ('gtfs', b'PK\x03\x04zip', True),
    ('gtfs', b'<!doctype html><html>err</html>', False),
    ('gtfs', b'plain text', False),
    ('gtfs', b'', False),
    ('netex', b'PK\x03\x04zip', True),
    ('netex', b'<?xml version="1.0"?><netex/>', True),
    ('netex', b'not xml or zip', False),
    ('other', b'anything goes', True),
    ('other', b'<html>err</html>', False),
])
def test_validate_static_feed_content(data_type, content, ok):
    if ok:
        validate_static_feed_content(data_type, content)
    else:
        with pytest.raises(InvalidFeedContent):
            validate_static_feed_content(data_type, content)


@pytest.mark.parametrize('protocol,content,ok', [
    ('gbfs', b'{"data": {}}', True),
    ('gbfs', b'not json', False),
    ('gtfs_rt', b'\x0a\x0bbinaryproto', True),
    ('gtfs_rt', b'<html>err</html>', False),
    ('siri', b'<?xml version="1.0"?><Siri/>', True),
    ('siri', b'<!DOCTYPE html><html>err</html>', False),
    ('siri', b'', False),
])
def test_validate_realtime_feed_content(protocol, content, ok):
    if ok:
        validate_realtime_feed_content(protocol, content)
    else:
        with pytest.raises(InvalidFeedContent):
            validate_realtime_feed_content(protocol, content)


def test_validate_uploaded_gtfs_zip_rewinds_stream():
    good = ContentFile(b'PK\x03\x04payload')
    validate_uploaded_gtfs_zip(good)
    assert good.read() == b'PK\x03\x04payload'  # stream rewound

    with pytest.raises(ValidationError):
        validate_uploaded_gtfs_zip(ContentFile(b'<html>nope</html>'))


# ---------------------------------------------------------------------------
# Content validation (integration): bad refresh must not clobber the cache
# ---------------------------------------------------------------------------

def test_bad_refresh_keeps_last_good_cache(settings, tmp_path, monkeypatch, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    entry = _entry(org, provider_user)
    entry.cached_file.save('gtfs.zip', ContentFile(b'PK\x03\x04good'), save=True)

    monkeypatch.setattr(
        'data_manager.scheduler.safe_get',
        lambda *a, **kw: _Response(b'<!doctype html><html>login page</html>'),
    )
    _fetch_static_entry(entry)

    entry.refresh_from_db()
    error = FeedFetchError.objects.get(static_entry=entry)
    assert error.error_type == FeedFetchError.ERROR_INVALID_CONTENT
    with entry.cached_file.open('rb') as fh:
        assert fh.read() == b'PK\x03\x04good'


# ---------------------------------------------------------------------------
# S7: RT cache filename derivation (no query-string leakage)
# ---------------------------------------------------------------------------

def _rt_endpoint(org, provider_user, url):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org, submitted_by=provider_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts, event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
        stage_before=3, stage_after=4, actor=provider_user,
    )
    return RealtimeEndpointRT.objects.create(
        submission=rts, endpoint_type='gbfs', url=url,
        hide_original=True, interval=30,
    )


def test_rt_cache_filename_drops_query_string(settings, tmp_path, monkeypatch, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    endpoint = _rt_endpoint(org, provider_user, 'https://example.org/api/gbfs.json?token=SECRET')

    monkeypatch.setattr(
        'data_manager.scheduler.safe_get', lambda *a, **kw: _Response(b'{"data": 1}'),
    )
    _fetch_realtime_endpoint_rt(endpoint, timezone.now())

    endpoint.refresh_from_db()
    assert endpoint.cached_file.name.endswith('/gbfs.json')
    assert 'SECRET' not in endpoint.cached_file.name


def test_rt_cache_filename_fallback_for_bare_path(settings, tmp_path, monkeypatch, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    endpoint = _rt_endpoint(org, provider_user, 'https://example.org/')

    monkeypatch.setattr(
        'data_manager.scheduler.safe_get', lambda *a, **kw: _Response(b'{"data": 1}'),
    )
    _fetch_realtime_endpoint_rt(endpoint, timezone.now())

    endpoint.refresh_from_db()
    assert endpoint.cached_file.name.endswith('/feed.pb')


# ---------------------------------------------------------------------------
# F4: orphaned-file cleanup
# ---------------------------------------------------------------------------

def test_deleting_entry_reclaims_files_and_report(settings, tmp_path, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    entry = _entry(org, provider_user)
    entry.cached_file.save('gtfs.zip', ContentFile(b'PK\x03\x04data'), save=True)

    report = FeedValidationReport.objects.create(error_count=0, warning_count=0)
    entry.validation_report = report
    entry.save(update_fields=['validation_report'])
    report.report_file.save('report.json', ContentFile(b'{}'), save=True)

    storage = entry.cached_file.storage
    cached_name = entry.cached_file.name
    report_name = report.report_file.name
    assert storage.exists(cached_name) and storage.exists(report_name)

    entry.delete()
    assert not storage.exists(cached_name)
    assert not storage.exists(report_name)
    assert not FeedValidationReport.objects.filter(pk=report.pk).exists()


def test_replacing_cached_file_with_new_name_deletes_old(settings, tmp_path, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    entry = _entry(org, provider_user)
    entry.cached_file.save('old-name.zip', ContentFile(b'PK\x03\x04old'), save=True)
    storage = entry.cached_file.storage
    old_name = entry.cached_file.name

    entry.cached_file.save('new-name.zip', ContentFile(b'PK\x03\x04new'), save=True)
    assert not storage.exists(old_name)
    assert storage.exists(entry.cached_file.name)


def test_same_name_rewrite_keeps_file(settings, tmp_path, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    entry = _entry(org, provider_user)
    entry.cached_file.save('gtfs.zip', ContentFile(b'PK\x03\x04v1'), save=True)

    entry.cached_file.save('gtfs.zip', ContentFile(b'PK\x03\x04v2'), save=True)
    entry.refresh_from_db()
    with entry.cached_file.open('rb') as fh:
        assert fh.read() == b'PK\x03\x04v2'


def test_scheduler_refresh_reclaims_renamed_cache(settings, tmp_path, monkeypatch, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    entry = _entry(org, provider_user)
    # Legacy cache stored under a name that embeds a query string (pre-S7).
    entry.cached_file.save('gtfs.zip?token=SECRET', ContentFile(b'PK\x03\x04old'), save=True)
    storage = entry.cached_file.storage
    legacy_name = entry.cached_file.name

    monkeypatch.setattr(
        'data_manager.scheduler.safe_get', lambda *a, **kw: _Response(b'PK\x03\x04fresh'),
    )
    monkeypatch.setattr('data_manager.tasks.validate_gtfs_feed_task.delay', lambda *a, **kw: None)
    _fetch_static_entry(entry)

    entry.refresh_from_db()
    assert entry.cached_file.name.endswith('/gtfs.zip')
    assert not storage.exists(legacy_name)


def test_deleting_rt_endpoint_reclaims_cache(settings, tmp_path, org, provider_user):
    settings.MEDIA_ROOT = tmp_path
    endpoint = _rt_endpoint(org, provider_user, 'https://example.org/gbfs.json')
    endpoint.cached_file.save('gbfs.json', ContentFile(b'{}'), save=True)
    storage = endpoint.cached_file.storage
    name = endpoint.cached_file.name

    endpoint.delete()
    assert not storage.exists(name)


# ---------------------------------------------------------------------------
# S1: production secret-key guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('bad_key', [
    None,
    '',
    '   ',
    'change-me',
    'changeme',
    'secret',
    'django-insecure-dev-placeholder',
    'django-insecure-' + 'x' * 64,   # long but generated with the dev prefix
    'short-key',                     # under 32 chars
    'x' * 31,
])
def test_weak_production_secret_keys_rejected(bad_key):
    with pytest.raises(ImproperlyConfigured):
        validate_production_secret_key(bad_key)


def test_strong_production_secret_key_accepted():
    validate_production_secret_key('k' * 64)


# ---------------------------------------------------------------------------
# Disk-growth regression: validator workdirs must be reclaimed on failure
# ---------------------------------------------------------------------------

def test_failed_validation_leaves_no_workdirs(settings, tmp_path, monkeypatch, org, provider_user):
    """Each validation run creates a random-suffix output dir; failures used to
    leak it, growing the disk on every retry (Docker down, broken report)."""
    import docker as docker_module

    from data_manager.tasks import validate_gtfs_feed_task

    settings.MEDIA_ROOT = tmp_path
    entry = _entry(org, provider_user, url=None, download_time_1=None, hide_original=False)
    entry.file.save('feed.zip', ContentFile(b'PK\x03\x04data'), save=True)

    def failing_from_env():
        raise docker_module.errors.DockerException('docker down')

    monkeypatch.setattr('docker.from_env', failing_from_env)
    monkeypatch.setenv('HOST_MEDIA_ROOT', str(tmp_path))

    validate_gtfs_feed_task(entry.id)

    leftovers = list(tmp_path.rglob('validation_report_*'))
    assert leftovers == [], f'leaked validator workdirs: {leftovers}'
