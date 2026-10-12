"""Public download views: publication gating, filename matching and the
deterministic /feed/rt/<pk>/<endpoint_type>/ route; static entries in the
public catalogue."""

from datetime import time

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def provider_user():
    user = get_user_model().objects.create_user('dl-provider', 'dl@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='DataProvider')
    user.groups.add(group)
    return user


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _make_submission(org, user, *, published=True):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=user,
        data_type='gtfs',
        name='Feed',
    )
    if published:
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=FeedSubmissionHistory.EVENT_COMPLETED,
            stage_before=3,
            stage_after=4,
            actor=user,
        )
    return submission


def _make_rt_submission(org, user, *, published=True, protocol=RealtimeSubmission.PROTOCOL_GBFS):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=user,
        protocol=protocol,
    )
    if published:
        RealtimeSubmissionHistory.objects.create(
            submission=rts,
            event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
            stage_before=3,
            stage_after=4,
            actor=user,
        )
    return rts


# ---------------------------------------------------------------------------
# Static: /feed/<pk>/...
# ---------------------------------------------------------------------------


def test_static_info_and_file_download(api_client, org, provider_user):
    submission = _make_submission(org, provider_user)
    entry = StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1='03:00',
    )
    entry.cached_file.save('gtfs.zip', ContentFile(b'PK\x03\x04zipbytes'), save=True)

    response = api_client.get(f'/feed/{submission.id}/')
    assert response.status_code == 200
    assert response.data['static'].endswith(f'/feed/{submission.id}/gtfs.zip')

    response = api_client.get(f'/feed/{submission.id}/gtfs.zip')
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'PK\x03\x04zipbytes'


def test_static_download_unpublished_is_404(api_client, org, provider_user):
    submission = _make_submission(org, provider_user, published=False)
    assert api_client.get(f'/feed/{submission.id}/').status_code == 404


def test_static_download_wrong_filename_is_404(api_client, org, provider_user):
    submission = _make_submission(org, provider_user)
    entry = StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1='03:00',
    )
    entry.cached_file.save('gtfs.zip', ContentFile(b'PK\x03\x04zipbytes'), save=True)
    assert api_client.get(f'/feed/{submission.id}/other.zip').status_code == 404


def test_static_prefers_cached_file_over_upload(api_client, org, provider_user):
    submission = _make_submission(org, provider_user)
    entry = StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1='03:00',
    )
    entry.file.save('upload.zip', ContentFile(b'PK\x03\x04old'), save=False)
    entry.cached_file.save('fresh.zip', ContentFile(b'PK\x03\x04fresh'), save=True)

    response = api_client.get(f'/feed/{submission.id}/')
    assert response.data['static'].endswith('/fresh.zip')


# ---------------------------------------------------------------------------
# Realtime: /feed/rt/<pk>/...
# ---------------------------------------------------------------------------


def _rt_with_colliding_basenames(org, provider_user):
    """Two endpoints whose source URLs share the basename 'feed.json'."""
    rts = _make_rt_submission(org, provider_user)
    a = RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='station_information',
        url='https://example.org/station/feed.json',
        hide_original=True,
        interval=30,
    )
    b = RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='station_status',
        url='https://example.org/status/feed.json',
        hide_original=True,
        interval=30,
    )
    a.cached_file.save('feed.json', ContentFile(b'{"kind": "information"}'), save=True)
    b.cached_file.save('feed.json', ContentFile(b'{"kind": "status"}'), save=True)
    return rts


def test_rt_info_lists_endpoint_type_urls(api_client, org, provider_user):
    rts = _rt_with_colliding_basenames(org, provider_user)
    response = api_client.get(f'/feed/rt/{rts.id}/')
    assert response.status_code == 200
    assert response.data['dynamic']['station_information'].endswith(
        f'/feed/rt/{rts.id}/station_information/'
    )
    assert response.data['dynamic']['station_status'].endswith(f'/feed/rt/{rts.id}/station_status/')


def test_rt_endpoint_type_route_is_deterministic(api_client, org, provider_user):
    rts = _rt_with_colliding_basenames(org, provider_user)

    response = api_client.get(f'/feed/rt/{rts.id}/station_information/')
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'{"kind": "information"}'

    response = api_client.get(f'/feed/rt/{rts.id}/station_status/')
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'{"kind": "status"}'


def test_rt_legacy_filename_route_still_serves(api_client, org, provider_user):
    rts = _make_rt_submission(org, provider_user)
    ep = RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.org/gbfs.json',
        hide_original=True,
        interval=30,
    )
    ep.cached_file.save('gbfs.json', ContentFile(b'{"data": 1}'), save=True)

    response = api_client.get(f'/feed/rt/{rts.id}/gbfs.json')
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'{"data": 1}'


def test_rt_unknown_endpoint_type_is_404(api_client, org, provider_user):
    rts = _rt_with_colliding_basenames(org, provider_user)
    assert api_client.get(f'/feed/rt/{rts.id}/free_bike_status/').status_code == 404


def test_rt_unpublished_is_404(api_client, org, provider_user):
    rts = _make_rt_submission(org, provider_user, published=False)
    assert api_client.get(f'/feed/rt/{rts.id}/').status_code == 404


# ---------------------------------------------------------------------------
# Public catalogue: /api/data_manager/feeds/<org>/
# ---------------------------------------------------------------------------


def test_catalogue_shows_refresh_times_only_for_proxied_static_feeds(
    api_client, org, provider_user
):
    proxied = _make_submission(org, provider_user)
    StaticFeedEntry.objects.create(
        submission=proxied,
        url='https://example.org/proxied.zip',
        hide_original=True,
        download_time_1=time(3, 0),
        download_time_2=time(15, 30),
    )
    direct = _make_submission(org, provider_user)
    StaticFeedEntry.objects.create(
        submission=direct,
        url='https://example.org/direct.zip',
        hide_original=False,
        download_time_1=time(4, 0),
    )

    response = api_client.get(f'/api/data_manager/feeds/{org.id}/')

    assert response.status_code == 200
    static_feeds = {feed['id']: feed['static_feed'] for feed in response.data['feeds']}
    assert static_feeds[proxied.id]['download_time_1'] == '03:00:00'
    assert static_feeds[proxied.id]['download_time_2'] == '15:30:00'
    # Not proxied: ODT never fetches it, so the stored times are not published.
    assert static_feeds[direct.id]['download_time_1'] is None
    assert static_feeds[direct.id]['download_time_2'] is None
