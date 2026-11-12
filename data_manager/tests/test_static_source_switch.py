"""Changing a static submission's source drops the proxy cache of the old one.

The cache (cached_file/cached_at) is the server's copy of a proxied URL. When
the source becomes a file, another URL or a non-proxied URL, the old copy
must neither block the edit nor keep being validated and served.
"""

import os
from datetime import time
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, FeedSubmissionHistory, StaticFeedEntry

pytestmark = pytest.mark.django_db

GTFS_PATH = os.path.join(os.path.dirname(__file__), 'GTFS_correct.zip')


def _user(username, group):
    user = get_user_model().objects.create_user(username, f'{username}@example.com', 'password')
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def owner():
    return _user('owner', 'DataProvider')


@pytest.fixture
def admin():
    return _user('admin-role', 'Admin')


@pytest.fixture
def helper():
    return _user('helper', 'Helper')


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _history(submission, event_type, stage_before, stage_after, actor):
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=event_type,
        stage_before=stage_before,
        stage_after=stage_after,
        cause='invalid feed' if event_type == FeedSubmissionHistory.EVENT_REJECTED else '',
        actor=actor,
    )


def _proxied_entry(org, owner, history=()):
    """A proxied URL submission with a cached copy on disk."""
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type='gtfs', name='Feed'
    )
    for event_type, before, after in history:
        _history(submission, event_type, before, after, owner)
    entry = StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1=time(3, 0),
    )
    entry.cached_file.save('cached.zip', ContentFile(b'PK\x03\x04cached'), save=False)
    StaticFeedEntry.objects.filter(pk=entry.pk).update(
        cached_file=entry.cached_file.name, cached_at=timezone.now()
    )
    entry.refresh_from_db()
    assert entry.cached_file.storage.exists(entry.cached_file.name)
    return submission, entry


REJECTED = [(FeedSubmissionHistory.EVENT_REJECTED, 2, 1)]
PUBLISHED = [(FeedSubmissionHistory.EVENT_COMPLETED, 3, 4)]


def _switch_to_file(api_client, submission):
    with open(GTFS_PATH, 'rb') as fh:
        return api_client.patch(
            f'/api/data_manager/feed-submissions/{submission.id}/',
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


@pytest.mark.parametrize('role', ['owner', 'admin'])
def test_rejected_proxied_url_can_switch_to_file(
    request, role, api_client, org, owner, django_capture_on_commit_callbacks
):
    submission, entry = _proxied_entry(org, owner, history=REJECTED)
    old_cache = entry.cached_file.name
    api_client.force_authenticate(user=request.getfixturevalue(role))

    with patch('data_manager.signals.validate_gtfs_feed_task.delay') as validate:
        with django_capture_on_commit_callbacks(execute=True):
            response = _switch_to_file(api_client, submission)

    assert response.status_code == 200, response.data
    _assert_cache_dropped(entry, old_cache)
    assert entry.file and not entry.url and not entry.hide_original
    # Validation runs on the new file, once.
    validate.assert_called_once_with(entry.pk)
    submission.refresh_from_db()
    expected_stage = 2 if role == 'owner' else 1  # only the owner resubmits
    assert submission.current_stage == expected_stage


def test_helper_cannot_switch_source_and_cache_stays(api_client, org, owner, helper):
    submission, entry = _proxied_entry(org, owner, history=REJECTED)
    old_cache = entry.cached_file.name
    api_client.force_authenticate(user=helper)

    response = _switch_to_file(api_client, submission)

    assert response.status_code == 403
    entry.refresh_from_db()
    assert entry.cached_file.name == old_cache
    assert entry.cached_file.storage.exists(old_cache)


def test_url_change_drops_cache_and_refetches(
    api_client, org, owner, django_capture_on_commit_callbacks
):
    submission, entry = _proxied_entry(org, owner)
    old_cache = entry.cached_file.name
    api_client.force_authenticate(user=owner)

    with patch('data_manager.tasks.fetch_static_entry_task.delay') as fetch:
        with django_capture_on_commit_callbacks(execute=True):
            response = api_client.patch(
                f'/api/data_manager/feed-submissions/{submission.id}/',
                {'static_entry': {'url': 'https://example.org/other.zip'}},
                format='json',
            )

    assert response.status_code == 200, response.data
    _assert_cache_dropped(entry, old_cache)
    fetch.assert_called_once_with(entry.pk)


def test_hide_original_off_stops_serving_the_cache(api_client, org, owner, admin):
    submission, entry = _proxied_entry(org, owner, history=PUBLISHED)
    old_cache = entry.cached_file.name
    assert api_client.get(f'/feed/{submission.id}/').data == {
        'static': f'http://testserver/feed/{submission.id}/cached.zip'
    }
    api_client.force_authenticate(user=admin)

    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'static_entry': {'hide_original': False}},
        format='json',
    )

    assert response.status_code == 200, response.data
    _assert_cache_dropped(entry, old_cache)
    api_client.force_authenticate(user=None)
    assert api_client.get(f'/feed/{submission.id}/').data == {}
    assert api_client.get(f'/feed/{submission.id}/cached.zip').status_code == 404


def test_schedule_change_keeps_the_cache(api_client, org, owner):
    submission, entry = _proxied_entry(org, owner, history=PUBLISHED)
    old_cache = entry.cached_file.name
    api_client.force_authenticate(user=owner)

    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'static_entry': {'download_time_1': '04:00:00'}},
        format='json',
    )

    assert response.status_code == 200, response.data
    entry.refresh_from_db()
    assert entry.cached_file.name == old_cache
    assert entry.cached_at is not None
    assert entry.cached_file.storage.exists(old_cache)
