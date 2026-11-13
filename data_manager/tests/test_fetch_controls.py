import importlib
import io
from contextlib import contextmanager
from datetime import timedelta

import pytest
import requests
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager import tasks
from data_manager.models import (
    AUTO_PAUSE_RETRY_DELAY,
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
from data_manager.scheduler import (
    FETCH_OK,
    FETCH_PERMANENT,
    FETCH_TRANSIENT,
    _fetch_realtime_endpoint_rt,
    _fetch_static_entry,
)
from data_manager.tests.test_flow import fake_download
from data_manager.validators import InvalidFeedContent

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
    user = get_user_model().objects.create_user(
        'helper-fetch', 'helper-fetch@example.com', 'password'
    )
    group, _ = Group.objects.get_or_create(name='Helper')
    user.groups.add(group)
    return user


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _is_daily_retry(feed):
    remaining = feed.next_fetch_after - timezone.now()
    return AUTO_PAUSE_RETRY_DELAY - timedelta(minutes=1) < remaining <= AUTO_PAUSE_RETRY_DELAY


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


def _published_realtime_endpoint(org, provider_user, stage=4):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=provider_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_COMPLETED
        if stage == 4
        else RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=stage - 1,
        stage_after=stage,
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
    settings,
    tmp_path,
    monkeypatch,
    org,
    provider_user,
):
    settings.MEDIA_ROOT = tmp_path
    entry = _published_static_entry(org, provider_user)

    @contextmanager
    def _timing_out(*args, **kwargs):
        raise requests.exceptions.Timeout('timeout')
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr('data_manager.scheduler.safe_download', _timing_out)

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
    assert _is_daily_retry(entry)
    assert FeedFetchError.objects.filter(static_entry=entry).count() == 4

    class Response:
        # Minimal ZIP magic: fetched GTFS content must look like a ZIP archive.
        content = b'PK\x03\x04feed-bytes'
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

    @contextmanager
    def _succeeding(*args, **kwargs):
        # safe_download yields (response, body_file) and streams the body.
        yield Response(), io.BytesIO(Response.content)

    monkeypatch.setattr('data_manager.scheduler.safe_download', _succeeding)
    monkeypatch.setattr(
        'data_manager.tasks.validate_gtfs_feed_task.delay', lambda *args, **kwargs: None
    )

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
    monkeypatch.setattr(
        tasks.fetch_static_entry_task, 'delay', lambda entry_id, **kwargs: calls.append(entry_id)
    )

    result = tasks.refresh_static_feeds_task()
    assert result['dispatched'] == 1
    assert calls == [due.id]


def _mock_static_download(monkeypatch, fail=False):
    """Count downloads; succeed with a minimal ZIP body or time out."""
    downloads = []

    class Response:
        headers = {}

        def raise_for_status(self):
            return None

    @contextmanager
    def _download(url, **kwargs):
        downloads.append(url)
        if fail:
            raise requests.exceptions.Timeout('timeout')
        yield Response(), io.BytesIO(b'PK\x03\x04feed-bytes')

    monkeypatch.setattr('data_manager.scheduler.safe_download', _download)
    monkeypatch.setattr('data_manager.tasks.validate_gtfs_feed_task.delay', lambda *a, **kw: None)
    return downloads


def _dispatch_ticks(monkeypatch, ticks):
    """Run the dispatcher `ticks` times; return the queued (args, kwargs)."""
    queued = []
    monkeypatch.setattr(
        tasks.fetch_static_entry_task,
        'delay',
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )
    for _ in range(ticks):
        tasks.refresh_static_feeds_task()
    return queued


def _run_queued(queued):
    return [tasks.fetch_static_entry_task(*args, **kwargs)['status'] for args, kwargs in queued]


def test_static_dispatch_ticks_before_first_fetch_download_once(monkeypatch, org, provider_user):
    _published_static_entry(org, provider_user)
    downloads = _mock_static_download(monkeypatch)

    # The feeds worker is busy: three ticks queue copies before any of them runs.
    queued = _dispatch_ticks(monkeypatch, 3)
    assert len(queued) == 3

    assert _run_queued(queued) == [FETCH_OK, 'already_fetched', 'already_fetched']
    assert len(downloads) == 1
    # Once the fetch succeeded the window stops dispatching it.
    assert _dispatch_ticks(monkeypatch, 1) == []


def test_static_retry_dispatch_ticks_download_once(monkeypatch, org, provider_user):
    entry = _published_static_entry(org, provider_user)
    entry.fetch_status = FETCH_STATUS_DELAYED
    entry.next_fetch_after = timezone.now() - timedelta(minutes=1)
    entry.save(update_fields=['fetch_status', 'next_fetch_after'])
    downloads = _mock_static_download(monkeypatch)

    queued = _dispatch_ticks(monkeypatch, 2)
    assert len(queued) == 2

    assert _run_queued(queued) == [FETCH_OK, 'already_fetched']
    assert len(downloads) == 1


def test_static_failed_fetch_is_retried_in_a_later_window(monkeypatch, org, provider_user):
    entry = _published_static_entry(org, provider_user)
    entry.last_fetch_success_at = timezone.now() - timedelta(days=1)
    entry.save(update_fields=['last_fetch_success_at'])
    downloads = _mock_static_download(monkeypatch, fail=True)

    queued = _dispatch_ticks(monkeypatch, 2)
    # The first copy fails and schedules a retry; the second waits for it.
    assert _run_queued(queued) == [FETCH_TRANSIENT, 'deferred']
    assert len(downloads) == 1

    entry.refresh_from_db()
    assert entry.fetch_status == FETCH_STATUS_DELAYED
    entry.next_fetch_after = timezone.now() - timedelta(seconds=1)
    entry.save(update_fields=['next_fetch_after'])
    downloads = _mock_static_download(monkeypatch)

    queued = _dispatch_ticks(monkeypatch, 1)
    assert _run_queued(queued) == [FETCH_OK]
    assert len(downloads) == 1
    entry.refresh_from_db()
    assert entry.fetch_status == FETCH_STATUS_ACTIVE


def test_direct_static_fetch_ignores_recent_success(monkeypatch, org, provider_user):
    # A changed URL queues the task without the dispatcher's cutoff.
    entry = _published_static_entry(org, provider_user)
    entry.mark_fetch_success()
    downloads = _mock_static_download(monkeypatch)

    assert tasks.fetch_static_entry_task(entry.id)['status'] == FETCH_OK
    assert len(downloads) == 1


def test_fetch_error_api_and_manual_static_pause_resume(
    api_client, org, provider_user, helper_user
):
    entry = _published_static_entry(org, provider_user)
    FeedFetchError.objects.create(
        static_entry=entry,
        error_type=FeedFetchError.ERROR_TIMEOUT,
        message='timeout',
        url_attempted=entry.url,
    )

    api_client.force_authenticate(user=provider_user)
    errors = api_client.get(
        f'/api/data_manager/feed-submissions/{entry.submission_id}/fetch-errors/?days=7'
    )
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


def test_realtime_pause_resume_schedules_endpoint(
    api_client, org, provider_user, helper_user, monkeypatch, django_capture_on_commit_callbacks
):
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

    with django_capture_on_commit_callbacks() as callbacks:
        resumed = api_client.post(
            f'/api/data_manager/realtime-endpoints/{endpoint.id}/resume-fetch/'
        )
    assert resumed.status_code == 200
    assert resumed.data['fetch_status'] == FETCH_STATUS_ACTIVE
    # The fetch loop is seeded only after the resume is committed.
    assert calls == []
    for callback in callbacks:
        callback()
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


def test_realtime_publish_seeds_fetch_loop_after_commit(
    api_client, org, provider_user, helper_user, monkeypatch, django_capture_on_commit_callbacks
):
    endpoint = _published_realtime_endpoint(org, provider_user, stage=3)
    calls = []
    monkeypatch.setattr(
        'data_manager.tasks.fetch_realtime_endpoint_task.apply_async',
        lambda *args, **kwargs: calls.append(args),
    )

    api_client.force_authenticate(user=helper_user)
    with django_capture_on_commit_callbacks() as callbacks:
        response = api_client.patch(
            f'/api/data_manager/realtime-submissions/{endpoint.submission_id}/',
            {'stage': 4},
            format='json',
        )

    assert response.status_code == 200, response.data
    assert calls == []
    for callback in callbacks:
        callback()
    assert len(calls) == 1


def test_global_fetch_errors_endpoint_filters_paginates_and_scopes(
    api_client,
    org,
    provider_user,
    helper_user,
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


def test_fetch_errors_link_to_their_submission_without_extra_queries(
    api_client, org, provider_user, helper_user, django_assert_max_num_queries
):
    static_entry = _published_static_entry(org, provider_user)
    rt_endpoint = _published_realtime_endpoint(org, provider_user)
    for _ in range(5):
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

    api_client.force_authenticate(user=helper_user)
    # Fixed budget: the submission comes from select_related, not one query per row.
    with django_assert_max_num_queries(4):
        response = api_client.get('/api/data_manager/fetch-errors/?days=7')

    assert response.status_code == 200
    assert response.data['count'] == 10
    assert {(row['source'], row['submission_id']) for row in response.data['results']} == {
        ('static', static_entry.submission_id),
        ('realtime', rt_endpoint.submission_id),
    }


def test_pause_and_resume_rejected_for_non_proxied_feeds(
    api_client, org, provider_user, helper_user, monkeypatch, django_capture_on_commit_callbacks
):
    """A non-proxied feed has no fetch loop to pause or resume."""
    static_entry = _published_static_entry(
        org,
        provider_user,
        hide_original=False,
        url='https://example.org/direct.zip',
    )
    rt_endpoint = _published_realtime_endpoint(org, provider_user)
    rt_endpoint.hide_original = False
    rt_endpoint.save(update_fields=['hide_original'])
    calls = []
    monkeypatch.setattr(
        'data_manager.tasks.fetch_realtime_endpoint_task.apply_async',
        lambda *args, **kwargs: calls.append(args),
    )

    api_client.force_authenticate(user=helper_user)
    for path in (
        f'/api/data_manager/static-feed-entries/{static_entry.id}',
        f'/api/data_manager/realtime-endpoints/{rt_endpoint.id}',
    ):
        paused = api_client.post(f'{path}/pause-fetch/')
        with django_capture_on_commit_callbacks(execute=True):
            resumed = api_client.post(f'{path}/resume-fetch/')

        for response in (paused, resumed):
            assert response.status_code == 400
            assert response.data == {'code': 'feeds.not_proxy_managed', 'details': {}}

    assert calls == []

    detail = api_client.get(f'/api/data_manager/static-feed-entries/{static_entry.id}/')
    assert detail.status_code == 200
    assert detail.data['is_proxy_managed'] is False
    assert 'fetch_status' not in detail.data


def test_create_non_proxied_url_triggers_validation_not_proxy_fetch(
    monkeypatch,
    org,
    provider_user,
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
    _published_static_entry(
        org, provider_user, hide_original=False, url='https://example.org/direct.zip'
    )
    non_proxy_rt = _published_realtime_endpoint(org, provider_user)
    non_proxy_rt.hide_original = False
    non_proxy_rt.save(update_fields=['hide_original'])

    filtered = api_client.get('/api/data_manager/proxy-feeds/')
    assert filtered.data['count'] == 2


def _proxy_feed_keys(api_client, query=''):
    response = api_client.get(f'/api/data_manager/proxy-feeds/{query}')
    assert response.status_code == 200
    keys = {(row['source'], row['id']) for row in response.data['results']}
    assert response.data['count'] == len(keys)
    return keys


def test_proxy_feeds_filters(api_client, org, provider_user, helper_user):
    other_org = TransportOrganization.objects.create(region='S', transport_organization='Org')
    static_entry = _published_static_entry(org, provider_user)
    paused_static = _published_static_entry(
        other_org, provider_user, fetch_status=FETCH_STATUS_AUTO_PAUSED
    )
    rt_endpoint = _published_realtime_endpoint(org, provider_user)
    other_rt = _published_realtime_endpoint(other_org, provider_user)
    every = {
        ('static', static_entry.id),
        ('static', paused_static.id),
        ('realtime', rt_endpoint.id),
        ('realtime', other_rt.id),
    }

    api_client.force_authenticate(user=helper_user)
    assert _proxy_feed_keys(api_client) == every
    assert _proxy_feed_keys(api_client, '?source=static') == {
        ('static', static_entry.id),
        ('static', paused_static.id),
    }
    assert _proxy_feed_keys(api_client, '?source=realtime') == {
        ('realtime', rt_endpoint.id),
        ('realtime', other_rt.id),
    }
    assert _proxy_feed_keys(api_client, f'?fetch_status={FETCH_STATUS_AUTO_PAUSED}') == {
        ('static', paused_static.id)
    }
    # Same organization name in two regions: the id tells them apart.
    assert _proxy_feed_keys(api_client, f'?organization={other_org.id}') == {
        ('static', paused_static.id),
        ('realtime', other_rt.id),
    }
    assert _proxy_feed_keys(api_client, '?region=r') == {
        ('static', static_entry.id),
        ('realtime', rt_endpoint.id),
    }
    assert _proxy_feed_keys(api_client, f'?source=realtime&organization={org.id}') == {
        ('realtime', rt_endpoint.id)
    }
    assert _proxy_feed_keys(api_client, '?fetch_status=active&source=static&region=S') == set()

    row = api_client.get(f'/api/data_manager/proxy-feeds/?organization={org.id}').data['results'][0]
    assert row['organization_id'] == org.id


def test_proxy_feeds_filters_keep_provider_scope(api_client, org, provider_user, helper_user):
    _published_static_entry(org, helper_user)
    own = _published_static_entry(org, provider_user)

    api_client.force_authenticate(user=provider_user)
    assert _proxy_feed_keys(api_client, f'?organization={org.id}&source=static') == {
        ('static', own.id)
    }


@pytest.mark.parametrize(
    ('query', 'field', 'code'),
    [
        ('?source=gtfs', 'source', 'feeds.invalid_choice'),
        ('?fetch_status=paused', 'fetch_status', 'feeds.invalid_choice'),
        ('?organization=Org', 'organization', 'feeds.invalid_integer'),
    ],
)
def test_proxy_feeds_invalid_filter_is_a_coded_400(api_client, helper_user, query, field, code):
    api_client.force_authenticate(user=helper_user)
    response = api_client.get(f'/api/data_manager/proxy-feeds/{query}')
    assert response.status_code == 400
    assert response.data == {field: [code]}


def test_proxy_feeds_return_last_fetch_error_type(api_client, org, provider_user, helper_user):
    static_entry = _published_static_entry(org, provider_user)
    rt_endpoint = _published_realtime_endpoint(org, provider_user)
    FeedFetchError.objects.create(
        static_entry=static_entry,
        error_type=FeedFetchError.ERROR_HTTP,
        http_status_code=500,
        message='older',
        url_attempted='https://example.org/feed.zip',
    )
    FeedFetchError.objects.create(
        static_entry=static_entry,
        error_type=FeedFetchError.ERROR_TIMEOUT,
        message='newer',
        url_attempted='https://example.org/feed.zip',
    )

    api_client.force_authenticate(user=helper_user)
    rows = {
        row['source']: row
        for row in api_client.get('/api/data_manager/proxy-feeds/').data['results']
    }
    assert rows['static']['last_fetch_error_type'] == FeedFetchError.ERROR_TIMEOUT
    assert rows['static']['last_fetch_error_message'] == 'newer'
    assert rows['realtime']['last_fetch_error_type'] is None

    detail = api_client.get(f'/api/data_manager/static-feed-entries/{static_entry.id}/')
    assert detail.status_code == 200
    assert detail.data['last_fetch_error_type'] == FeedFetchError.ERROR_TIMEOUT
    endpoint = api_client.get(f'/api/data_manager/realtime-endpoints/{rt_endpoint.id}/')
    assert endpoint.status_code == 200
    assert endpoint.data['last_fetch_error_type'] is None
    # Nested in the submission, the entry is not annotated and takes the fallback.
    submission = api_client.get(f'/api/data_manager/feed-submissions/{static_entry.submission_id}/')
    assert submission.status_code == 200
    assert submission.data['static_entry']['last_fetch_error_type'] == FeedFetchError.ERROR_TIMEOUT


# Content-Length promises 1000 bytes; the server sends 10 and hangs up.
TRUNCATED = (1000, [b'PK\x03\x04part'])


def test_truncated_static_body_is_a_transient_connection_error(local_server, org, provider_user):
    entry = _published_static_entry(org, provider_user, url=local_server(*TRUNCATED))

    assert _fetch_static_entry(entry) == FETCH_TRANSIENT
    error = FeedFetchError.objects.get(static_entry=entry)
    assert error.error_type == FeedFetchError.ERROR_CONNECTION


def test_truncated_realtime_body_is_a_transient_connection_error(local_server, org, provider_user):
    endpoint = _published_realtime_endpoint(org, provider_user)
    endpoint.url = local_server(*TRUNCATED)
    endpoint.save(update_fields=['url'])

    assert _fetch_realtime_endpoint_rt(endpoint, timezone.now()) == FETCH_TRANSIENT
    error = FeedFetchError.objects.get(endpoint_rt=endpoint)
    assert error.error_type == FeedFetchError.ERROR_CONNECTION


def _download_pausing(model, pk, body=b'', exc=None):
    """safe_download stand-in: a Helper pauses the feed while it is in flight."""

    @contextmanager
    def _download(*args, **kwargs):
        model.objects.get(pk=pk).pause_fetch('maintenance')
        if exc is not None:
            raise exc
        with fake_download(body)() as result:
            yield result

    return _download


@pytest.mark.parametrize('fails', [False, True], ids=['success', 'failure'])
def test_static_pause_during_fetch_survives_its_outcome(monkeypatch, org, provider_user, fails):
    entry = _published_static_entry(org, provider_user)
    monkeypatch.setattr(
        'data_manager.scheduler.safe_download',
        _download_pausing(
            StaticFeedEntry,
            entry.pk,
            body=b'PK\x03\x04feed-bytes',
            exc=requests.exceptions.Timeout('timeout') if fails else None,
        ),
    )
    monkeypatch.setattr('data_manager.tasks.validate_gtfs_feed_task.delay', lambda *a, **k: None)

    result = tasks.fetch_static_entry_task(entry.pk)

    entry.refresh_from_db()
    assert result['fetch_status'] == FETCH_STATUS_MANUAL_PAUSED
    assert entry.fetch_status == FETCH_STATUS_MANUAL_PAUSED
    assert entry.fetch_pause_reason == 'maintenance'
    assert entry.fetch_failure_count == 0
    assert entry.next_fetch_after is None
    # The outcome itself is still recorded.
    if fails:
        assert entry.last_fetch_error_at is not None
        assert entry.last_fetch_success_at is None
    else:
        assert entry.last_fetch_success_at is not None
        assert entry.last_fetch_error_at is None


@pytest.mark.parametrize('fails', [False, True], ids=['success', 'failure'])
def test_realtime_pause_during_fetch_survives_and_stops_loop(
    monkeypatch, org, provider_user, fails
):
    endpoint = _published_realtime_endpoint(org, provider_user)
    monkeypatch.setattr(
        'data_manager.scheduler.safe_download',
        _download_pausing(
            RealtimeEndpointRT,
            endpoint.pk,
            body=b'{}',
            exc=requests.exceptions.ConnectionError('down') if fails else None,
        ),
    )
    monkeypatch.setattr(
        'data_manager.scheduler.validate_realtime_feed_stream', lambda *a, **k: set()
    )
    rescheduled = []
    monkeypatch.setattr(
        tasks.fetch_realtime_endpoint_task,
        'apply_async',
        lambda *a, **k: rescheduled.append(k),
    )

    result = tasks.fetch_realtime_endpoint_task(endpoint.pk)

    endpoint.refresh_from_db()
    assert result['fetch_status'] == FETCH_STATUS_MANUAL_PAUSED
    assert endpoint.fetch_status == FETCH_STATUS_MANUAL_PAUSED
    assert endpoint.fetch_failure_count == 0
    assert rescheduled == []
    if fails:
        assert endpoint.last_fetch_error_at is not None
    else:
        assert endpoint.last_fetch_success_at is not None


def test_proxy_feeds_pages_list_every_feed_once_with_polish_names(
    api_client, org, provider_user, helper_user
):
    # Polish letters and mixed case sort differently under the database's
    # linguistic collation and Python's code-point order; paging must still
    # return each feed exactly once, in the order of the single-page list.
    expected = set()
    for region in ['śląskie', 'łódzkie', 'lubelskie', 'Mazowieckie', 'mazowieckie', 'zachodnie']:
        region_org = TransportOrganization.objects.create(
            region=region, transport_organization=f'Ździebło {region}'
        )
        static_entry = _published_static_entry(region_org, provider_user)
        rt_endpoint = _published_realtime_endpoint(region_org, provider_user)
        expected |= {('static', static_entry.id), ('realtime', rt_endpoint.id)}

    api_client.force_authenticate(user=helper_user)
    url = '/api/data_manager/proxy-feeds/?page_size=2'
    paged = []
    while url:
        response = api_client.get(url)
        assert response.status_code == 200
        assert response.data['count'] == len(expected)
        paged.extend((row['source'], row['id']) for row in response.data['results'])
        url = response.data['next']

    single_page = api_client.get('/api/data_manager/proxy-feeds/?page_size=100')
    assert len(paged) == len(set(paged))
    assert set(paged) == expected
    assert paged == [(row['source'], row['id']) for row in single_page.data['results']]


def _raising_download(exc):
    @contextmanager
    def _download(*args, **kwargs):
        raise exc
        yield  # pragma: no cover - unreachable, keeps this a generator

    return _download


def _http_error(code):
    response = requests.Response()
    response.status_code = code
    return requests.exceptions.HTTPError(str(code), response=response)


LONG_LADDER = [timedelta(minutes=5), timedelta(hours=1), timedelta(hours=6), timedelta(days=1)]
REALTIME_TRANSIENT_LADDER = [
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(days=1),
]


def _assert_ladder(feed, fetch, ladder):
    """Fail ``feed`` once per step: each step's delay, then auto-pause and a daily retry."""
    for count, delay in enumerate(ladder, start=1):
        started = timezone.now()
        fetch()
        feed.refresh_from_db()
        assert feed.fetch_failure_count == count
        expected = FETCH_STATUS_DELAYED if count < len(ladder) else FETCH_STATUS_AUTO_PAUSED
        assert feed.fetch_status == expected
        assert started + delay <= feed.next_fetch_after <= timezone.now() + delay


@pytest.mark.parametrize(
    'exc',
    [
        requests.exceptions.Timeout('timeout'),
        requests.exceptions.ConnectionError('refused'),
        _http_error(503),
        _http_error(429),
    ],
    ids=['timeout', 'connection', 'http-503', 'http-429'],
)
def test_realtime_transient_errors_use_the_short_ladder(monkeypatch, org, provider_user, exc):
    endpoint = _published_realtime_endpoint(org, provider_user)
    monkeypatch.setattr('data_manager.scheduler.safe_download', _raising_download(exc))

    def fetch():
        assert _fetch_realtime_endpoint_rt(endpoint, timezone.now()) == FETCH_TRANSIENT

    _assert_ladder(endpoint, fetch, REALTIME_TRANSIENT_LADDER)


@pytest.mark.parametrize(
    'exc',
    [_http_error(404), _http_error(401), InvalidFeedContent('realtime.invalid')],
    ids=['http-404', 'http-401', 'invalid-content'],
)
def test_realtime_permanent_errors_keep_the_long_ladder(monkeypatch, org, provider_user, exc):
    endpoint = _published_realtime_endpoint(org, provider_user)
    monkeypatch.setattr('data_manager.scheduler.safe_download', _raising_download(exc))

    def fetch():
        assert _fetch_realtime_endpoint_rt(endpoint, timezone.now()) == FETCH_PERMANENT

    _assert_ladder(endpoint, fetch, LONG_LADDER)


def test_realtime_ladder_follows_the_latest_error(monkeypatch, org, provider_user):
    endpoint = _published_realtime_endpoint(org, provider_user)
    monkeypatch.setattr(
        'data_manager.scheduler.safe_download',
        _raising_download(requests.exceptions.Timeout('timeout')),
    )
    _fetch_realtime_endpoint_rt(endpoint, timezone.now())
    _fetch_realtime_endpoint_rt(endpoint, timezone.now())
    monkeypatch.setattr('data_manager.scheduler.safe_download', _raising_download(_http_error(404)))

    started = timezone.now()
    _fetch_realtime_endpoint_rt(endpoint, started)

    endpoint.refresh_from_db()
    assert endpoint.fetch_failure_count == 3
    assert endpoint.next_fetch_after >= started + timedelta(hours=6)


@pytest.mark.parametrize('code', [503, 429, 404])
def test_static_errors_keep_the_long_ladder(monkeypatch, org, provider_user, code):
    entry = _published_static_entry(org, provider_user)
    monkeypatch.setattr(
        'data_manager.scheduler.safe_download', _raising_download(_http_error(code))
    )

    _assert_ladder(entry, lambda: _fetch_static_entry(entry), LONG_LADDER)


def _auto_paused(feed, next_fetch_after):
    feed.fetch_status = FETCH_STATUS_AUTO_PAUSED
    feed.fetch_failure_count = 4
    feed.fetch_pause_reason = 'timeout'
    feed.next_fetch_after = next_fetch_after
    feed.save(
        update_fields=[
            'fetch_status',
            'fetch_failure_count',
            'fetch_pause_reason',
            'next_fetch_after',
        ]
    )


def test_auto_paused_static_entry_waits_for_its_daily_retry(monkeypatch, org, provider_user):
    entry = _published_static_entry(org, provider_user)
    _auto_paused(entry, timezone.now() + timedelta(hours=1))
    _mock_static_download(monkeypatch)

    # Not even at its download time: the daily retry is the only attempt.
    assert _dispatch_ticks(monkeypatch, 1) == []
    assert tasks.fetch_static_entry_task(entry.pk)['status'] == 'deferred'


@pytest.mark.parametrize('fails', [False, True], ids=['success', 'failure'])
def test_auto_paused_static_entry_is_retried_daily(monkeypatch, org, provider_user, fails):
    entry = _published_static_entry(org, provider_user)
    _auto_paused(entry, timezone.now() - timedelta(seconds=1))
    downloads = _mock_static_download(monkeypatch, fail=fails)

    _run_queued(_dispatch_ticks(monkeypatch, 1))

    assert len(downloads) == 1
    entry.refresh_from_db()
    if fails:
        assert entry.fetch_status == FETCH_STATUS_AUTO_PAUSED
        assert entry.fetch_failure_count == 5
        assert entry.fetch_pause_reason == 'timeout'
        assert _is_daily_retry(entry)
    else:
        assert entry.fetch_status == FETCH_STATUS_ACTIVE
        assert entry.fetch_failure_count == 0
        assert entry.next_fetch_after is None
        assert entry.fetch_pause_reason == ''


def test_manually_paused_feeds_are_never_retried(monkeypatch, org, provider_user, celery_send_task):
    entry = _published_static_entry(org, provider_user)
    endpoint = _published_realtime_endpoint(org, provider_user)
    for feed in (entry, endpoint):
        feed.pause_fetch('maintenance')
        # Even a stray past retry time does not bring a manual pause back.
        type(feed).objects.filter(pk=feed.pk).update(
            next_fetch_after=timezone.now() - timedelta(days=2)
        )
    _mock_static_download(monkeypatch)

    assert _dispatch_ticks(monkeypatch, 1) == []
    assert tasks.fetch_static_entry_task(entry.pk)['status'] == 'deferred'
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 0
    assert celery_send_task.call_count == 0


def test_migration_schedules_daily_retry_of_feeds_auto_paused_before(org, provider_user):
    migration = importlib.import_module('data_manager.migrations.0017_schedule_auto_paused_retry')
    entry = _published_static_entry(org, provider_user)
    endpoint = _published_realtime_endpoint(org, provider_user)
    manual = _published_static_entry(org, provider_user, url='https://example.org/manual.zip')
    manual.pause_fetch('maintenance')
    for feed in (entry, endpoint):
        _auto_paused(feed, None)

    migration.schedule_daily_retry(apps, None)

    for feed in (entry, endpoint):
        feed.refresh_from_db()
        assert feed.fetch_is_due()
    manual.refresh_from_db()
    assert manual.next_fetch_after is None
