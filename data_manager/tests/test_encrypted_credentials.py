"""Stored feed credentials fail closed: no plaintext in production, no ciphertext
sent upstream, no prefixed input stored as is, and the key can be rotated."""

import io
import logging
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import connection

from cases.models import TransportOrganization
from data_manager import fields
from data_manager import validation_errors as ve
from data_manager.fields import (
    CREDENTIAL_UNDECRYPTABLE,
    CredentialUndecryptable,
    UndecryptableValue,
)
from data_manager.models import (
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)
from data_manager.scheduler import (
    FETCH_PERMANENT,
    _build_auth_headers,
    _fetch_realtime_endpoint_rt,
    _fetch_static_entry,
)
from data_manager.tasks import validate_gtfs_feed_task, validate_realtime_submission_task
from OtwarteDaneTransportowe.settings_base import validate_feed_auth_encryption_keys

pytestmark = pytest.mark.django_db

KEY_0 = Fernet.generate_key().decode()
KEY_1 = Fernet.generate_key().decode()
FIELD = StaticFeedEntry._meta.get_field('auth_value')


@pytest.fixture(autouse=True)
def keys(settings):
    fields._log_once.cache_clear()
    settings.FEED_AUTH_ENCRYPTION_KEY = KEY_0
    settings.FEED_AUTH_ENCRYPTION_OLD_KEYS = []
    return settings


@pytest.fixture
def user():
    user = get_user_model().objects.create_user('provider', 'p@example.com', 'password')
    user.groups.add(Group.objects.get_or_create(name='DataProvider')[0])
    return user


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.HOST_MEDIA_ROOT = str(tmp_path)
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _entry(org, user, stage=4, **kwargs):
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=user, data_type='gtfs', name='F'
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=stage,
    )
    values = {
        'url': 'https://example.org/gtfs.zip',
        'hide_original': True,
        'download_time_1': '03:00',
        'auth_type': 'api_key',
        'auth_value': 'top-secret',
        **kwargs,
    }
    return StaticFeedEntry.objects.create(submission=submission, **values)


def _endpoint(org, user, **kwargs):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org, submitted_by=user, protocol='gbfs'
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
        actor=user,
    )
    values = {
        'url': 'https://example.org/gbfs.json',
        'endpoint_type': 'gbfs',
        'hide_original': True,
        'auth_type': 'api_key',
        'auth_value': 'rt-secret',
        **kwargs,
    }
    return RealtimeEndpointRT.objects.create(submission=rts, **values)


def _stored(obj) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT auth_value FROM {obj._meta.db_table} WHERE id = %s',
            [obj.pk],
        )
        return cursor.fetchone()[0]


def _decrypt_with(key, stored):
    return Fernet(key).decrypt(stored.removeprefix('fernet$').encode()).decode()


# ---------------------------------------------------------------------------
# The field
# ---------------------------------------------------------------------------


def test_round_trip_stores_only_ciphertext(org, user):
    entry = _entry(org, user)

    assert 'top-secret' not in _stored(entry)
    assert _decrypt_with(KEY_0, _stored(entry)) == 'top-secret'
    entry.refresh_from_db()
    assert entry.auth_value == 'top-secret'


def test_legacy_plaintext_row_is_read_unchanged(org, user):
    entry = _entry(org, user)
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE {entry._meta.db_table} SET auth_value = %s WHERE id = %s',
            ['legacy-plain', entry.pk],
        )

    entry.refresh_from_db()

    assert entry.auth_value == 'legacy-plain'


def test_prefixed_input_is_encrypted_like_any_other(org, user):
    entry = _entry(org, user, auth_value='fernet$not-a-token')

    assert _decrypt_with(KEY_0, _stored(entry)) == 'fernet$not-a-token'
    entry.refresh_from_db()
    assert entry.auth_value == 'fernet$not-a-token'


def test_wrong_key_reads_an_undecryptable_value_not_the_ciphertext(keys, org, user, caplog):
    entry = _entry(org, user)
    stored = _stored(entry)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1

    with caplog.at_level(logging.ERROR, logger='data_manager.fields'):
        entry.refresh_from_db()
        StaticFeedEntry.objects.get(pk=entry.pk)

    assert isinstance(entry.auth_value, UndecryptableValue)
    assert stored not in repr(entry.auth_value)
    # Logged once per process, without the value.
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert stored not in caplog.text


def test_dumpdata_refuses_an_undecryptable_value(keys, org, user):
    _entry(org, user)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1

    with pytest.raises(CommandError, match=CREDENTIAL_UNDECRYPTABLE):
        call_command('dumpdata', 'data_manager.StaticFeedEntry', stdout=io.StringIO())


def test_no_key_reads_a_stored_token_as_undecryptable(keys, org, user):
    entry = _entry(org, user)
    keys.FEED_AUTH_ENCRYPTION_KEY = None

    entry.refresh_from_db()

    assert isinstance(entry.auth_value, UndecryptableValue)


def test_saving_an_undecryptable_value_keeps_the_stored_token(keys, org, user):
    entry = _entry(org, user)
    stored = _stored(entry)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1
    entry.refresh_from_db()

    # Model.full_clean() (admin, API) runs to_python on the attribute.
    entry.auth_value = FIELD.clean(entry.auth_value, entry)
    entry.save()

    assert _stored(entry) == stored
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_0
    entry.refresh_from_db()
    assert entry.auth_value == 'top-secret'


def test_old_keys_decrypt_and_writes_use_the_current_key(keys, org, user):
    entry = _entry(org, user)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1
    keys.FEED_AUTH_ENCRYPTION_OLD_KEYS = [KEY_0]

    entry.refresh_from_db()
    assert entry.auth_value == 'top-secret'

    entry.save()
    assert _decrypt_with(KEY_1, _stored(entry)) == 'top-secret'


def test_no_key_stores_plaintext_and_warns_once(keys, org, user, caplog):
    keys.FEED_AUTH_ENCRYPTION_KEY = None

    with caplog.at_level(logging.WARNING, logger='data_manager.fields'):
        first = _entry(org, user, auth_value='dev-1')
        _entry(org, user, auth_value='dev-2')

    assert _stored(first) == 'dev-1'
    assert len(caplog.records) == 1
    assert 'dev-1' not in caplog.text


# ---------------------------------------------------------------------------
# Every reader of the credential
# ---------------------------------------------------------------------------


def test_auth_headers_refuse_an_undecryptable_value():
    with pytest.raises(CredentialUndecryptable):
        _build_auth_headers('api_key', UndecryptableValue('fernet$x'))
    # Without an auth type the credential is not used.
    assert _build_auth_headers('', UndecryptableValue('fernet$x')) == {}


def test_static_fetch_fails_with_a_coded_auth_error(keys, org, user):
    entry = _entry(org, user)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1
    entry.refresh_from_db()

    with patch('data_manager.scheduler.safe_download') as download:
        assert _fetch_static_entry(entry) == FETCH_PERMANENT

    download.assert_not_called()
    error = FeedFetchError.objects.get(static_entry=entry)
    assert (error.error_type, error.message) == (
        FeedFetchError.ERROR_AUTH,
        CREDENTIAL_UNDECRYPTABLE,
    )
    entry.refresh_from_db()
    assert entry.fetch_pause_reason == CREDENTIAL_UNDECRYPTABLE


def test_realtime_fetch_fails_with_a_coded_auth_error(keys, org, user):
    endpoint = _endpoint(org, user)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1
    endpoint.refresh_from_db()

    with patch('data_manager.scheduler.safe_download') as download:
        result = _fetch_realtime_endpoint_rt(endpoint, now=None)

    assert result == FETCH_PERMANENT
    download.assert_not_called()
    error = FeedFetchError.objects.get(endpoint_rt=endpoint)
    assert (error.error_type, error.message) == (
        FeedFetchError.ERROR_AUTH,
        CREDENTIAL_UNDECRYPTABLE,
    )


def test_gtfs_validation_download_is_an_infra_error_not_a_rejection(keys, org, user):
    entry = _entry(org, user, stage=2, hide_original=False)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1

    with patch('data_manager.net_security.safe_download') as download:
        validate_gtfs_feed_task(entry.id)

    download.assert_not_called()
    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_ERROR
    assert entry.validation_message == ve.CREDENTIAL_UNDECRYPTABLE
    assert entry.submission.current_stage == 2


def test_realtime_verification_stays_at_stage_2(keys, org, user):
    endpoint = _endpoint(org, user)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1

    with patch('data_manager.net_security.safe_get') as safe_get:
        result = validate_realtime_submission_task(endpoint.submission_id)

    safe_get.assert_not_called()
    assert result == {'status': CREDENTIAL_UNDECRYPTABLE, 'id': endpoint.submission_id}
    endpoint.submission.refresh_from_db()
    assert endpoint.submission.current_stage == 2


def test_realtime_edit_keeps_an_undecryptable_credential(keys, org, user):
    from rest_framework.test import APIClient

    endpoint = _endpoint(org, user)
    stored = _stored(endpoint)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1
    client = APIClient()
    client.force_authenticate(user=user)

    with patch('data_manager.api.serializers.assert_safe_outbound_url'):
        response = client.patch(
            f'/api/data_manager/realtime-submissions/{endpoint.submission_id}/',
            {'endpoints': [{'endpoint_type': 'gbfs', 'url': endpoint.url, 'auth_type': 'api_key'}]},
            format='json',
        )

    assert response.status_code == 200, response.data
    assert _stored(RealtimeEndpointRT.objects.get(submission=endpoint.submission)) == stored


# ---------------------------------------------------------------------------
# manage.py rotate_feed_auth_key
# ---------------------------------------------------------------------------


def test_rotation_reencrypts_every_credential_with_the_current_key(keys, org, user):
    keys.FEED_AUTH_ENCRYPTION_KEY = None
    legacy = _entry(org, user, auth_value='legacy-plain')
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_0
    entry = _entry(org, user)
    endpoint = _endpoint(org, user)
    no_credential = _entry(org, user, auth_type='', auth_value=None)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1
    keys.FEED_AUTH_ENCRYPTION_OLD_KEYS = [KEY_0]

    call_command('rotate_feed_auth_key')

    assert _decrypt_with(KEY_1, _stored(legacy)) == 'legacy-plain'
    assert _decrypt_with(KEY_1, _stored(entry)) == 'top-secret'
    assert _decrypt_with(KEY_1, _stored(endpoint)) == 'rt-secret'
    assert _stored(no_credential) is None


def test_rotation_reports_undecryptable_rows_and_keeps_them(keys, org, user):
    lost = _entry(org, user)
    lost_stored = _stored(lost)
    keys.FEED_AUTH_ENCRYPTION_KEY = KEY_1
    kept = _entry(org, user, auth_value='kept')
    keys.FEED_AUTH_ENCRYPTION_KEY = Fernet.generate_key().decode()
    keys.FEED_AUTH_ENCRYPTION_OLD_KEYS = [KEY_1]

    with pytest.raises(CommandError, match=f'data_manager.StaticFeedEntry.auth_value id={lost.pk}'):
        call_command('rotate_feed_auth_key')

    assert _stored(lost) == lost_stored
    keys.FEED_AUTH_ENCRYPTION_OLD_KEYS = []
    kept.refresh_from_db()
    assert kept.auth_value == 'kept'


def test_rotation_needs_a_key(keys):
    keys.FEED_AUTH_ENCRYPTION_KEY = None

    with pytest.raises(CommandError, match='FEED_AUTH_ENCRYPTION_KEY'):
        call_command('rotate_feed_auth_key')


# ---------------------------------------------------------------------------
# settings_prod
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('key', 'old_keys', 'name'),
    [
        (None, [], 'FEED_AUTH_ENCRYPTION_KEY'),
        ('', [], 'FEED_AUTH_ENCRYPTION_KEY'),
        ('change-me', [], 'FEED_AUTH_ENCRYPTION_KEY'),
        (KEY_0, ['not-a-key'], 'FEED_AUTH_ENCRYPTION_OLD_KEYS'),
    ],
)
def test_production_refuses_a_missing_or_malformed_key(key, old_keys, name):
    with pytest.raises(ImproperlyConfigured, match=name):
        validate_feed_auth_encryption_keys(key, old_keys)


def test_production_accepts_fernet_keys():
    validate_feed_auth_encryption_keys(KEY_1, [KEY_0])
