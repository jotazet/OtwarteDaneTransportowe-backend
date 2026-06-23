from datetime import timedelta

import pytest
import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager import tasks
from data_manager.models import (
    FETCH_STATUS_ACTIVE,
    FETCH_STATUS_AUTO_PAUSED,
    FETCH_STATUS_DELAYED,
    FETCH_STATUS_MANUAL_PAUSED,
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)
from data_manager.scheduler import FETCH_OK, _fetch_static_entry


pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def provider_user():
    user = get_user_model().objects.create_user('provider', 'provider@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='DataProvider')
    user.groups.add(group)
    return user


@pytest.fixture
def helper_user():
    user = get_user_model().objects.create_user('helper-fetch', 'helper-fetch@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='Helper')
    user.groups.add(group)
    return user


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _published_static_entry(org, provider_user, **entry_kwargs):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=provider_user,
        data_type='gtfs',
        name='Static',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=provider_user,
    )
    defaults = {
        'url': 'https://example.org/gtfs.zip',
        'hide_original': True,
        'download_time_1': timezone.now().time().replace(second=0, microsecond=0),
    }
    defaults.update(entry_kwargs)
    return StaticFeedEntry.objects.create(submission=submission, **defaults)


def _published_realtime_endpoint(org, provider_user):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=provider_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=provider_user,
    )
    return RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.org/gbfs.json',
        hide_original=True,
        interval=30,
    )


def test_static_fetch_errors_apply_pause_sequence_and_success_resets(
    settings, tmp_path, monkeypatch, org, provider_user,
):
    settings.MEDIA_ROOT = tmp_path
    entry = _published_static_entry(org, provider_user)

    monkeypatch.setattr(
        'data_manager.scheduler.safe_get',
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.exceptions.Timeout('timeout')),
    )

    first_started = timezone.now()
    _fetch_static_entry(entry)
    entry.refresh_from_db()
    assert entry.fetch_status == FETCH_STATUS_DELAYED
    assert entry.fetch_failure_count == 1
    assert 250 <= (entry.next_fetch_after - first_started).total_seconds() <= 310

    _fetch_static_entry(entry)
    entry.refresh_from_db()
    assert entry.fetch_status == FETCH_STATUS_DELAYED
    assert entry.fetch_failure_count == 2
    assert 3500 <= (entry.next_fetch_after - timezone.now()).total_seconds() <= 3610

    _fetch_static_entry(entry)
    entry.refresh_from_db()
    assert entry.fetch_status == FETCH_STATUS_DELAYED
    assert entry.fetch_failure_count == 3
    assert 21500 <= (entry.next_fetch_after - timezone.now()).total_seconds() <= 21610

    _fetch_static_entry(entry)
    entry.refresh_from_db()
    assert entry.fetch_status == FETCH_STATUS_AUTO_PAUSED
    assert entry.fetch_failure_count == 4
    assert entry.next_fetch_after is None
    assert FeedFetchError.objects.filter(static_entry=entry).count() == 4

    class Response:
        content = b'feed-bytes'

        def raise_for_status(self):
            return None

    monkeypatch.setattr('data_manager.scheduler.safe_get', lambda *args, **kwargs: Response())
    monkeypatch.setattr('data_manager.tasks.validate_gtfs_feed_task.delay', lambda *args, **kwargs: None)

    result = _fetch_static_entry(entry)
    entry.refresh_from_db()
    assert result == FETCH_OK
    assert entry.fetch_status == FETCH_STATUS_ACTIVE
    assert entry.fetch_failure_count == 0
    assert entry.next_fetch_after is None
    assert entry.last_fetch_success_at is not None


def test_static_dispatch_skips_paused_and_future_delayed_entries(monkeypatch, org, provider_user):
    due = _published_static_entry(org, provider_user, url='https://example.org/due.zip')
    manual = _published_static_entry(org, provider_user, url='https://example.org/manual.zip')
    manual.pause_fetch('maintenance')
    future = _published_static_entry(org, provider_user, url='https://example.org/future.zip')
    future.fetch_status = FETCH_STATUS_DELAYED
    future.next_fetch_after = timezone.now() + timedelta(hours=1)
    future.save(update_fields=['fetch_status', 'next_fetch_after'])

    calls = []
    monkeypatch.setattr(tasks.fetch_static_entry_task, 'delay', lambda entry_id: calls.append(entry_id))

    result = tasks.refresh_static_feeds_task()
    assert result['dispatched'] == 1
    assert calls == [due.id]


def test_fetch_error_api_and_manual_static_pause_resume(api_client, org, provider_user, helper_user):
    entry = _published_static_entry(org, provider_user)
    FeedFetchError.objects.create(
        static_entry=entry,
        error_type=FeedFetchError.ERROR_TIMEOUT,
        message='timeout',
        url_attempted=entry.url,
    )

    api_client.force_authenticate(user=provider_user)
    errors = api_client.get(f'/api/data_manager/feed-submissions/{entry.submission_id}/fetch-errors/?days=7')
    assert errors.status_code == 200
    assert errors.data['count'] == 1
    row = errors.data['results'][0]
    assert row['error_type'] == FeedFetchError.ERROR_TIMEOUT
    assert row['organization'] == 'Org'
    assert row['feed_name'] == f'#{entry.submission_id} Static'

    paused_view = api_client.get(f'/api/data_manager/static-feed-entries/{entry.id}/')
    assert paused_view.status_code == 200
    assert paused_view.data['organization'] == 'Org'
    assert paused_view.data['feed_name'] == f'#{entry.submission_id} Static'

    forbidden = api_client.post(f'/api/data_manager/static-feed-entries/{entry.id}/pause-fetch/')
    assert forbidden.status_code == 403

    api_client.force_authenticate(user=helper_user)
    paused = api_client.post(
        f'/api/data_manager/static-feed-entries/{entry.id}/pause-fetch/',
        {'reason': 'operator pause'},
        format='json',
    )
    assert paused.status_code == 200
    assert paused.data['fetch_status'] == FETCH_STATUS_MANUAL_PAUSED

    resumed = api_client.post(f'/api/data_manager/static-feed-entries/{entry.id}/resume-fetch/')
    assert resumed.status_code == 200
    assert resumed.data['fetch_status'] == FETCH_STATUS_ACTIVE
    assert resumed.data['fetch_failure_count'] == 0


def test_realtime_pause_resume_schedules_endpoint(api_client, org, provider_user, helper_user, monkeypatch):
    endpoint = _published_realtime_endpoint(org, provider_user)
    calls = []
    monkeypatch.setattr(
        'data_manager.tasks.fetch_realtime_endpoint_task.apply_async',
        lambda *args, **kwargs: calls.append(kwargs),
    )

    api_client.force_authenticate(user=helper_user)
    paused = api_client.post(
        f'/api/data_manager/realtime-endpoints/{endpoint.id}/pause-fetch/',
        {'reason': 'bad upstream'},
        format='json',
    )
    assert paused.status_code == 200
    assert paused.data['fetch_status'] == FETCH_STATUS_MANUAL_PAUSED

    resumed = api_client.post(f'/api/data_manager/realtime-endpoints/{endpoint.id}/resume-fetch/')
    assert resumed.status_code == 200
    assert resumed.data['fetch_status'] == FETCH_STATUS_ACTIVE
    assert len(calls) == 1

    FeedFetchError.objects.create(
        endpoint_rt=endpoint,
        error_type=FeedFetchError.ERROR_CONNECTION,
        message='connection failed',
        url_attempted=endpoint.url,
    )
    errors = api_client.get(
        f'/api/data_manager/realtime-submissions/{endpoint.submission_id}/fetch-errors/'
        f'?days=7&endpoint_type={endpoint.endpoint_type}'
    )
    assert errors.status_code == 200
    assert errors.data['count'] == 1
    assert errors.data['results'][0]['endpoint_type'] == endpoint.endpoint_type


def test_global_fetch_errors_endpoint_filters_paginates_and_scopes(
    api_client, org, provider_user, helper_user,
):
    static_entry = _published_static_entry(org, provider_user)
    rt_endpoint = _published_realtime_endpoint(org, provider_user)

    for _ in range(3):
        FeedFetchError.objects.create(
            static_entry=static_entry,
            error_type=FeedFetchError.ERROR_TIMEOUT,
            message='timeout',
            url_attempted=static_entry.url,
        )
    FeedFetchError.objects.create(
        endpoint_rt=rt_endpoint,
        error_type=FeedFetchError.ERROR_CONNECTION,
        message='connection failed',
        url_attempted=rt_endpoint.url,
    )

    # Another provider's feed must not leak into a non-operator listing.
    other_provider = get_user_model().objects.create_user(
        'other-provider', 'other@example.com', 'password'
    )
    other_provider.groups.add(Group.objects.get_or_create(name='DataProvider')[0])
    other_entry = _published_static_entry(org, other_provider)
    FeedFetchError.objects.create(
        static_entry=other_entry,
        error_type=FeedFetchError.ERROR_HTTP,
        http_status_code=503,
        message='unavailable',
        url_attempted=other_entry.url,
    )

    # Operator sees everything (5 total) and pagination caps the page size.
    api_client.force_authenticate(user=helper_user)
    page1 = api_client.get('/api/data_manager/fetch-errors/?days=7&page_size=2')
    assert page1.status_code == 200
    assert page1.data['count'] == 5
    assert len(page1.data['results']) == 2
    assert page1.data['next'] is not None

    # Source filter narrows to realtime only.
    rt_only = api_client.get('/api/data_manager/fetch-errors/?days=7&source=realtime')
    assert rt_only.data['count'] == 1
    rt_row = rt_only.data['results'][0]
    assert rt_row['source'] == 'realtime'
    assert rt_row['organization'] == 'Org'
    assert rt_row['feed_name'] == f'#{rt_endpoint.submission_id} '.strip()

    # error_type filter.
    timeouts = api_client.get('/api/data_manager/fetch-errors/?days=7&error_type=timeout')
    assert timeouts.data['count'] == 3

    # Provider only sees their own feeds' errors (3 static + 1 realtime = 4).
    api_client.force_authenticate(user=provider_user)
    scoped = api_client.get('/api/data_manager/fetch-errors/?days=7')
    assert scoped.data['count'] == 4


def test_pause_rejected_for_non_proxied_feeds(api_client, org, provider_user, helper_user):
    static_entry = _published_static_entry(
        org, provider_user, hide_original=False, url='https://example.org/direct.zip',
    )
    rt_endpoint = _published_realtime_endpoint(org, provider_user)
    rt_endpoint.hide_original = False
    rt_endpoint.save(update_fields=['hide_original'])

    api_client.force_authenticate(user=helper_user)
    static_pause = api_client.post(
        f'/api/data_manager/static-feed-entries/{static_entry.id}/pause-fetch/',
    )
    assert static_pause.status_code == 400

    rt_pause = api_client.post(
        f'/api/data_manager/realtime-endpoints/{rt_endpoint.id}/pause-fetch/',
    )
    assert rt_pause.status_code == 400

    detail = api_client.get(f'/api/data_manager/static-feed-entries/{static_entry.id}/')
    assert detail.status_code == 200
    assert detail.data['is_proxy_managed'] is False
    assert 'fetch_status' not in detail.data


def test_create_non_proxied_url_triggers_validation_not_proxy_fetch(
    monkeypatch, org, provider_user,
):
    fetch_calls = []
    validate_calls = []
    monkeypatch.setattr(
        'data_manager.tasks.fetch_static_entry_task.delay',
        lambda entry_id: fetch_calls.append(entry_id),
    )
    monkeypatch.setattr(
        'data_manager.tasks.validate_gtfs_feed_task.delay',
        lambda entry_id: validate_calls.append(entry_id),
    )

    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=provider_user,
        data_type='gtfs',
        name='Direct URL',
    )
    entry = StaticFeedEntry(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=False,
        download_time_1=timezone.now().time().replace(second=0, microsecond=0),
    )
    entry.full_clean()
    entry.save()
    if entry.url:
        if entry.is_proxy_managed:
            from data_manager.tasks import fetch_static_entry_task
            fetch_static_entry_task.delay(entry.id)
        elif entry.submission.data_type == 'gtfs':
            from data_manager.tasks import validate_gtfs_feed_task
            validate_gtfs_feed_task.delay(entry.id)

    assert fetch_calls == []
    assert validate_calls == [entry.id]


def test_proxy_feeds_list_combines_static_and_realtime(api_client, org, provider_user, helper_user):
    static_entry = _published_static_entry(org, provider_user)
    static_entry.submission.name = 'Static GTFS'
    static_entry.submission.save(update_fields=['name'])
    rt_endpoint = _published_realtime_endpoint(org, provider_user)
    rt_endpoint.submission.name = 'GBFS City'
    rt_endpoint.submission.save(update_fields=['name'])

    api_client.force_authenticate(user=helper_user)
    response = api_client.get('/api/data_manager/proxy-feeds/')
    assert response.status_code == 200
    assert response.data['count'] == 2
    sources = {row['source'] for row in response.data['results']}
    assert sources == {'static', 'realtime'}

    static_row = next(r for r in response.data['results'] if r['source'] == 'static')
    assert static_row['id'] == static_entry.id
    assert static_row['organization'] == 'Org'
    assert static_row['region'] == 'R'
    assert static_row['feed_name'] == f'#{static_entry.submission_id} Static GTFS'
    assert static_row['data_type'] == 'gtfs'

    rt_row = next(r for r in response.data['results'] if r['source'] == 'realtime')
    assert rt_row['id'] == rt_endpoint.id
    assert rt_row['organization'] == 'Org'
    assert rt_row['region'] == 'R'
    assert rt_row['feed_name'] == f'#{rt_endpoint.submission_id} GBFS City'
    assert rt_row['protocol'] == 'gbfs'
    assert rt_row['endpoint_type'] == 'gbfs'

    # Non-proxy feeds are excluded.
    _published_static_entry(org, provider_user, hide_original=False, url='https://example.org/direct.zip')
    non_proxy_rt = _published_realtime_endpoint(org, provider_user)
    non_proxy_rt.hide_original = False
    non_proxy_rt.save(update_fields=['hide_original'])

    filtered = api_client.get('/api/data_manager/proxy-feeds/')
    assert filtered.data['count'] == 2
