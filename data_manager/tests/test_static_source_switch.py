"""Changing a static submission's source drops the proxy cache of the old one.

The cache (cached_file/cached_at) is the server's copy of a proxied URL. When
the source becomes a file, another URL or a non-proxied URL, the old copy
must neither block the edit nor keep being validated and served.
"""

import os
from datetime import time
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, FeedSubmissionHistory, StaticFeedEntry
from OtwarteDaneTransportowe.auth_roles import ROLE_ADMIN, ROLE_DATA_PROVIDER, ROLE_HELPER

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('fake_dns')]

GTFS_PATH = os.path.join(os.path.dirname(__file__), 'GTFS_correct.zip')
REJECTED = (FeedSubmissionHistory.EVENT_REJECTED, 2, 1)
PUBLISHED = (FeedSubmissionHistory.EVENT_COMPLETED, 3, 4)


@pytest.fixture
def owner(make_user):
    return make_user(ROLE_DATA_PROVIDER, 'owner')


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _proxied_entry(org, owner, history=None):
    """A proxied URL submission with a cached copy on disk."""
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type='gtfs', name='Feed'
    )
    if history:
        event_type, stage_before, stage_after = history
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=event_type,
            stage_before=stage_before,
            stage_after=stage_after,
            actor=owner,
        )
    entry = StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1=time(3, 0),
        cached_at=timezone.now(),
    )
    entry.cached_file.save('cached.zip', ContentFile(b'PK\x03\x04cached'), save=True)
    return submission, entry


def _patch(client, submission, data, format='json'):
    return client.patch(f'/api/data_manager/feed-submissions/{submission.id}/', data, format=format)


def _switch_to_file(client, submission):
    with open(GTFS_PATH, 'rb') as fh:
        return _patch(
            client,
            submission,
            {
                'static_entry.file': fh,
                'static_entry.url': '',
                'static_entry.hide_original': 'false',
                'static_entry.download_time_1': '',
            },
            format='multipart',
        )


def _assert_cache_dropped(entry, old_cache_name):
    storage = entry.cached_file.storage
    entry.refresh_from_db()
    assert not entry.cached_file
    assert entry.cached_at is None
    assert not storage.exists(old_cache_name)


def _assert_cache_kept(entry, old_cache_name):
    entry.refresh_from_db()
    assert entry.cached_file.name == old_cache_name
    assert entry.cached_at is not None
    assert entry.cached_file.storage.exists(old_cache_name)


@pytest.mark.parametrize('role', ['owner', ROLE_ADMIN])
def test_rejected_proxied_url_can_switch_to_file(
    role, org, owner, make_user, client_as, django_capture_on_commit_callbacks
):
    submission, entry = _proxied_entry(org, owner, history=REJECTED)
    old_cache = entry.cached_file.name
    client = client_as(owner if role == 'owner' else make_user(role))

    with patch('data_manager.signals.validate_gtfs_feed_task.delay') as validate:
        with django_capture_on_commit_callbacks(execute=True):
            response = _switch_to_file(client, submission)

    assert response.status_code == 200, response.data
    _assert_cache_dropped(entry, old_cache)
    assert entry.file and not entry.url and not entry.hide_original
    # Validation runs on the new file, once.
    validate.assert_called_once_with(entry.pk)
    submission.refresh_from_db()
    expected_stage = 2 if role == 'owner' else 1  # only the owner resubmits
    assert submission.current_stage == expected_stage


def test_helper_cannot_switch_source_and_cache_stays(org, owner, make_user, client_as):
    submission, entry = _proxied_entry(org, owner, history=REJECTED)
    old_cache = entry.cached_file.name

    response = _switch_to_file(client_as(make_user(ROLE_HELPER)), submission)

    assert response.status_code == 403
    _assert_cache_kept(entry, old_cache)


def test_url_change_drops_cache_and_refetches(
    org, owner, client_as, django_capture_on_commit_callbacks
):
    submission, entry = _proxied_entry(org, owner)
    old_cache = entry.cached_file.name

    with patch('data_manager.tasks.fetch_static_entry_task.delay') as fetch:
        with django_capture_on_commit_callbacks(execute=True):
            response = _patch(
                client_as(owner),
                submission,
                {'static_entry': {'url': 'https://example.org/other.zip'}},
            )

    assert response.status_code == 200, response.data
    _assert_cache_dropped(entry, old_cache)
    fetch.assert_called_once_with(entry.pk)


def test_hide_original_off_stops_serving_the_cache(org, owner, make_user, client_as):
    submission, entry = _proxied_entry(org, owner, history=PUBLISHED)
    old_cache = entry.cached_file.name
    anonymous = client_as(None)
    assert anonymous.get(f'/feed/{submission.id}/').data == {
        'static': f'http://testserver/feed/{submission.id}/cached.zip'
    }

    response = _patch(
        client_as(make_user(ROLE_ADMIN)),
        submission,
        {'static_entry': {'hide_original': False}},
    )

    assert response.status_code == 200, response.data
    _assert_cache_dropped(entry, old_cache)
    assert anonymous.get(f'/feed/{submission.id}/').data == {}
    assert anonymous.get(f'/feed/{submission.id}/cached.zip').status_code == 404


def test_schedule_change_keeps_the_cache(org, owner, client_as):
    submission, entry = _proxied_entry(org, owner, history=PUBLISHED)
    old_cache = entry.cached_file.name

    response = _patch(
        client_as(owner), submission, {'static_entry': {'download_time_1': '04:00:00'}}
    )

    assert response.status_code == 200, response.data
    _assert_cache_kept(entry, old_cache)


def test_fetch_finishing_after_a_switch_does_not_restore_the_cache(monkeypatch, org, owner):
    import io
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from data_manager.scheduler import FETCH_PERMANENT, _fetch_static_entry

    submission, entry = _proxied_entry(org, owner)
    stale = StaticFeedEntry.objects.get(pk=entry.pk)  # what the fetch task loaded

    @contextmanager
    def download_while_switching(*args, **kwargs):
        # The owner switches to a file while the body is still downloading.
        StaticFeedEntry.objects.filter(pk=entry.pk).update(
            url='', hide_original=False, cached_file='', cached_at=None
        )
        yield MagicMock(), io.BytesIO(b'PK\x03\x04late')

    monkeypatch.setattr('data_manager.scheduler.safe_download', download_while_switching)
    monkeypatch.setattr('data_manager.scheduler.validate_static_feed_stream', lambda *a: None)

    assert _fetch_static_entry(stale) == FETCH_PERMANENT

    entry.refresh_from_db()
    assert not entry.cached_file and entry.cached_at is None
    assert not entry.cached_file.storage.exists(stale.cached_file.name)
