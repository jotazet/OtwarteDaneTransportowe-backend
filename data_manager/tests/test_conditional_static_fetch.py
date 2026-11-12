"""Scheduled static fetches skip an upstream file identical to the cached copy.

A ``304`` to a conditional request, or a ``200`` with the same SHA-256, is a
successful fetch that keeps the cache and queues no GTFS validation.
"""

import hashlib
import io
from contextlib import contextmanager
from datetime import time, timedelta
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone
from kombu.exceptions import OperationalError

from cases.models import TransportOrganization
from data_manager.models import (
    FETCH_STATUS_ACTIVE,
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    StaticFeedEntry,
)
from data_manager.scheduler import FETCH_OK, _fetch_static_entry
from OtwarteDaneTransportowe.auth_roles import ROLE_DATA_PROVIDER

pytestmark = pytest.mark.django_db

CACHED = b'PK\x03\x04cached-feed'
ETAG = '"v1"'
LAST_MODIFIED = 'Wed, 11 Nov 2026 03:00:00 GMT'


class _Response:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        return None


@pytest.fixture
def entry(settings, tmp_path, make_user):
    """A published proxied feed whose cached copy came with validators."""
    settings.MEDIA_ROOT = tmp_path
    owner = make_user(ROLE_DATA_PROVIDER, 'owner')
    org = TransportOrganization.objects.create(region='R', transport_organization='Org')
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type='gtfs', name='Feed'
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=owner,
    )
    entry = StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1=time(3, 0),
        cached_at=timezone.now() - timedelta(days=1),
        cached_etag=ETAG,
        cached_last_modified=LAST_MODIFIED,
        cached_sha256=hashlib.sha256(CACHED).hexdigest(),
        validation_status=StaticFeedEntry.VALIDATION_VALID,
    )
    entry.cached_file.save('gtfs.zip', ContentFile(CACHED), save=True)
    return entry


@pytest.fixture
def upstream(monkeypatch):
    """Fake safe_download: set ``.response`` / ``.body``; read ``.headers`` sent."""

    class Upstream:
        response = _Response()
        body = b''
        headers = None

    @contextmanager
    def download(url, *, headers=None, **kwargs):
        Upstream.headers = headers
        yield Upstream.response, io.BytesIO(Upstream.body)

    monkeypatch.setattr('data_manager.scheduler.safe_download', download)
    return Upstream


@pytest.fixture
def validate():
    with patch('data_manager.tasks.validate_gtfs_feed_task.delay') as delay:
        yield delay


def _cached_bytes(entry):
    with entry.cached_file.open('rb') as fh:
        return fh.read()


def _assert_cache_kept(entry, cached_at):
    entry.refresh_from_db()
    assert entry.cached_at == cached_at
    assert _cached_bytes(entry) == CACHED
    assert entry.last_fetch_success_at is not None


def test_not_modified_keeps_cache_and_skips_validation(entry, upstream, validate):
    cached_at = entry.cached_at
    upstream.response = _Response(304, {'ETag': '"v2"'})

    assert _fetch_static_entry(entry) == FETCH_OK

    assert upstream.headers == {'If-None-Match': ETAG, 'If-Modified-Since': LAST_MODIFIED}
    _assert_cache_kept(entry, cached_at)
    assert entry.cached_etag == '"v2"'
    assert entry.cached_last_modified == LAST_MODIFIED
    validate.assert_not_called()


def test_same_body_without_304_keeps_cache_and_skips_validation(entry, upstream, validate):
    cached_at = entry.cached_at
    upstream.response = _Response(200, {'ETag': '"v2"'})
    upstream.body = CACHED

    assert _fetch_static_entry(entry) == FETCH_OK

    _assert_cache_kept(entry, cached_at)
    assert entry.cached_etag == '"v2"'
    validate.assert_not_called()


def test_unfinished_validation_is_retried_for_an_unchanged_file(entry, upstream, validate):
    StaticFeedEntry.objects.filter(pk=entry.pk).update(
        validation_status=StaticFeedEntry.VALIDATION_ERROR
    )
    entry.refresh_from_db()
    upstream.response = _Response(304)

    assert _fetch_static_entry(entry) == FETCH_OK

    validate.assert_called_once_with(entry.pk)


def test_changed_body_replaces_cache_and_validates(entry, upstream, validate):
    new_body = b'PK\x03\x04new-feed'
    upstream.response = _Response(
        200, {'ETag': '"v2"', 'Last-Modified': 'Thu, 12 Nov 2026 03:00:00 GMT'}
    )
    upstream.body = new_body

    assert _fetch_static_entry(entry) == FETCH_OK

    entry.refresh_from_db()
    assert _cached_bytes(entry) == new_body
    assert entry.cached_sha256 == hashlib.sha256(new_body).hexdigest()
    assert entry.cached_etag == '"v2"'
    assert entry.cached_last_modified == 'Thu, 12 Nov 2026 03:00:00 GMT'
    assert entry.cached_at == entry.last_fetch_success_at
    validate.assert_called_once_with(entry.pk)


def test_new_copy_without_validators_drops_the_old_ones(entry, upstream, validate):
    """Otherwise a rollback upstream to the old file would answer 304."""
    upstream.body = b'PK\x03\x04new-feed'

    assert _fetch_static_entry(entry) == FETCH_OK

    entry.refresh_from_db()
    assert (entry.cached_etag, entry.cached_last_modified) == ('', '')


def test_source_refresh_downloads_and_validates_an_unchanged_file(
    entry, upstream, validate, django_capture_on_commit_callbacks
):
    """A resubmission keeps its URL and cache, but must be validated again."""
    from data_manager.api.serializers import FeedSubmissionWriteSerializer

    upstream.body = CACHED
    with patch('data_manager.tasks.fetch_static_entry_task.delay') as fetch:
        with django_capture_on_commit_callbacks(execute=True):
            FeedSubmissionWriteSerializer._schedule_source_refresh(entry)
    fetch.assert_called_once_with(entry.pk)
    entry.refresh_from_db()

    assert _fetch_static_entry(entry) == FETCH_OK

    assert upstream.headers == {}
    validate.assert_called_once_with(entry.pk)


def test_no_conditional_request_without_a_cached_copy(entry, upstream, validate):
    StaticFeedEntry.objects.filter(pk=entry.pk).update(cached_file='', cached_at=None)
    entry.refresh_from_db()
    upstream.body = CACHED

    assert _fetch_static_entry(entry) == FETCH_OK

    assert upstream.headers == {}
    entry.refresh_from_db()
    assert _cached_bytes(entry) == CACHED
    validate.assert_called_once_with(entry.pk)


def test_broker_error_after_a_fetch_is_not_a_fetch_failure(entry, upstream, validate):
    """The new copy is cached; only queuing its validation failed (issue #47)."""
    validate.side_effect = OperationalError('broker unreachable')
    upstream.body = b'PK\x03\x04new-feed'

    assert _fetch_static_entry(entry) == FETCH_OK

    validate.assert_called_once_with(entry.pk)
    entry.refresh_from_db()
    assert _cached_bytes(entry) == upstream.body
    assert entry.fetch_status == FETCH_STATUS_ACTIVE
    assert entry.fetch_failure_count == 0
    assert not FeedFetchError.objects.filter(static_entry=entry).exists()
