"""Regression tests for the realtime self-scheduling loop.

Guards against the previous behaviour where the periodic ``bootstrap_realtime_tasks``
re-queued every active endpoint on each run, accumulating duplicate fetch chains.
"""

import importlib
from datetime import timedelta

import pytest
from celery.schedules import crontab
from django.apps import apps
from django.core.cache import cache
from django.utils import timezone

from cases.models import TransportOrganization
from data_manager import tasks
from data_manager.models import (
    FETCH_RETRY_DELAYS,
    FETCH_STATUS_ACTIVE,
    FETCH_STATUS_AUTO_PAUSED,
    FETCH_STATUS_DELAYED,
    REALTIME_TRANSIENT_RETRY_DELAYS,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)

pytestmark = pytest.mark.django_db

min_interval_migration = importlib.import_module(
    'data_manager.migrations.0015_realtime_endpoint_min_interval'
)


@pytest.fixture
def published_gbfs_endpoint(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    org = TransportOrganization.objects.create(region='R', transport_organization='Org')
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
    )
    endpoint = RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.org/gbfs.json',
        hide_original=True,
        interval=30,
    )
    return endpoint


def test_bootstrap_does_not_duplicate_live_chains(published_gbfs_endpoint, monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks.fetch_realtime_endpoint_task,
        'apply_async',
        lambda *args, **kwargs: calls.append(kwargs),
    )

    first = tasks.bootstrap_realtime_tasks()
    second = tasks.bootstrap_realtime_tasks()

    # First run seeds the endpoint; second run sees a live alive-marker and skips.
    assert first['bootstrapped'] == 1
    assert second['bootstrapped'] == 0
    assert second['skipped_alive'] == 1
    assert len(calls) == 1


def test_bootstrap_reseeds_after_marker_expires(published_gbfs_endpoint, monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks.fetch_realtime_endpoint_task,
        'apply_async',
        lambda *args, **kwargs: calls.append(kwargs),
    )

    tasks.bootstrap_realtime_tasks()
    # Simulate a dead chain: its alive-marker expired / was cleared.
    cache.delete(tasks._rt_alive_key(published_gbfs_endpoint.id))

    again = tasks.bootstrap_realtime_tasks()
    assert again['bootstrapped'] == 1
    assert len(calls) == 2


def test_schedule_stops_and_releases_reservation_when_broker_is_down(
    published_gbfs_endpoint, monkeypatch
):
    """A failed enqueue must not leave the alive-marker behind: it would make
    the periodic bootstrap skip the endpoint until the marker expires. Nor may
    the request wait on the dead broker once per endpoint."""
    second = RealtimeEndpointRT.objects.create(
        submission=published_gbfs_endpoint.submission,
        endpoint_type='station_status',
        url='https://example.org/station_status.json',
        hide_original=True,
        interval=30,
    )
    attempts = []

    def broker_down(*args, **kwargs):
        attempts.append(args)
        raise OSError('Error 111 connecting to redis:6379. Connection refused.')

    monkeypatch.setattr(tasks.fetch_realtime_endpoint_task, 'apply_async', broker_down)

    scheduled = tasks.schedule_realtime_endpoint_fetches(published_gbfs_endpoint.submission_id)

    assert scheduled == 0
    assert len(attempts) == 1
    for endpoint in (published_gbfs_endpoint, second):
        assert cache.get(tasks._rt_alive_key(endpoint.id)) is None


def test_fetch_task_clamps_stored_interval_below_minimum(published_gbfs_endpoint, monkeypatch):
    """A row stored before the 5 s minimum existed must not loop with countdown 0."""
    from data_manager import scheduler

    RealtimeEndpointRT.objects.filter(pk=published_gbfs_endpoint.pk).update(interval=0)
    monkeypatch.setattr(scheduler, '_fetch_realtime_endpoint_rt', lambda endpoint, now: 'ok')
    calls = []
    monkeypatch.setattr(
        tasks.fetch_realtime_endpoint_task,
        'apply_async',
        lambda *args, **kwargs: calls.append(kwargs),
    )

    tasks.fetch_realtime_endpoint_task(published_gbfs_endpoint.id)

    assert [call['countdown'] for call in calls] == [5]


def test_migration_raises_stored_intervals_below_minimum(published_gbfs_endpoint):
    RealtimeEndpointRT.objects.filter(pk=published_gbfs_endpoint.pk).update(interval=0)

    min_interval_migration.raise_short_intervals(apps, None)

    published_gbfs_endpoint.refresh_from_db()
    assert published_gbfs_endpoint.interval == 5


@pytest.fixture
def queued(celery_send_task):
    """Realtime fetch messages sent so far, as (args, countdown); a callable
    because celery_send_task records calls as they happen."""

    def sent():
        return [
            (call.args[1], call.kwargs.get('countdown'))
            for call in celery_send_task.call_args_list
            if call.args[0] == tasks.fetch_realtime_endpoint_task.name
        ]

    return sent


@pytest.fixture
def fetches(monkeypatch):
    """Fake upstream fetch that succeeds unless ``fetches.fail`` is set; a
    failure backs off on the ``fetches.delays`` ladder."""

    def fake_fetch(endpoint, now):
        fake_fetch.calls += 1
        if fake_fetch.fail:
            endpoint.mark_fetch_failure('upstream down', now=now, delays=fake_fetch.delays)
            return 'error'
        endpoint.mark_fetch_success(now=now)
        return 'ok'

    fake_fetch.calls = 0
    fake_fetch.fail = False
    fake_fetch.delays = FETCH_RETRY_DELAYS
    monkeypatch.setattr('data_manager.scheduler._fetch_realtime_endpoint_rt', fake_fetch)
    return fake_fetch


def _replay(message_args):
    """Run a queued fetch message, as a worker would."""
    return tasks.fetch_realtime_endpoint_task(*message_args)


def test_stale_token_exits_without_fetching_or_rescheduling(
    published_gbfs_endpoint, queued, fetches
):
    cache.set(tasks._rt_alive_key(published_gbfs_endpoint.id), 'current-token')

    result = _replay([published_gbfs_endpoint.id, 'old-token'])

    assert result['status'] == 'stale'
    assert fetches.calls == 0
    assert queued() == []
    assert cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id)) == 'current-token'


def test_message_that_outlived_its_marker_keeps_the_loop(published_gbfs_endpoint, queued, fetches):
    """Queue lag past the marker's TTL with no competing loop: the late message
    re-claims the slot instead of leaving the feed idle until the next bootstrap."""
    tasks.bootstrap_realtime_tasks()
    cache.delete(tasks._rt_alive_key(published_gbfs_endpoint.id))  # marker expired

    assert _replay(queued()[-1][0])['status'] == 'ok'
    assert len(queued()) == 2
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 0


def test_each_run_hands_a_fresh_token_to_its_successor(published_gbfs_endpoint, queued, fetches):
    """A redelivered copy of an already executed message (Redis visibility
    timeout) carries the old token and must not fork the loop."""
    tasks.bootstrap_realtime_tasks()
    ((seed_args, _),) = queued()

    first = _replay(seed_args)
    assert first['next_in'] == published_gbfs_endpoint.interval
    next_args, countdown = queued()[-1]
    assert next_args[0] == published_gbfs_endpoint.id
    assert next_args[1] != seed_args[1]
    assert countdown == published_gbfs_endpoint.interval

    assert _replay(seed_args)['status'] == 'stale'
    assert fetches.calls == 1
    assert len(queued()) == 2


def test_bootstrap_after_marker_expiry_leaves_one_effective_loop(
    published_gbfs_endpoint, queued, fetches
):
    """Scenario: the queue lags past the alive marker's TTL, bootstrap seeds a
    second loop, and the old loop's message still runs afterwards."""
    tasks.bootstrap_realtime_tasks()
    _replay(queued()[-1][0])
    old_loop_message = queued()[-1][0]

    cache.delete(tasks._rt_alive_key(published_gbfs_endpoint.id))  # marker expired
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 1
    new_loop_message = queued()[-1][0]

    assert _replay(old_loop_message)['status'] == 'stale'
    assert _replay(new_loop_message)['status'] == 'ok'
    assert fetches.calls == 2
    # Only the new loop rescheduled itself.
    assert len(queued()) == 4
    assert queued()[-1][0][1] == cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id))


def test_delayed_endpoint_is_not_queued_and_resumes_through_bootstrap(
    published_gbfs_endpoint, queued, fetches
):
    tasks.bootstrap_realtime_tasks()
    fetches.fail = True
    result = _replay(queued()[-1][0])

    # The back-off (5 min, 1 h, 6 h) is never queued as a long ETA.
    assert result['fetch_status'] == FETCH_STATUS_DELAYED
    assert len(queued()) == 1
    assert cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id)) is None

    # Not due yet: bootstrap leaves it alone.
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 0

    RealtimeEndpointRT.objects.filter(pk=published_gbfs_endpoint.pk).update(
        next_fetch_after=timezone.now() - timedelta(seconds=1)
    )
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 1
    fetches.fail = False
    resumed = _replay(queued()[-1][0])
    assert resumed['fetch_status'] == FETCH_STATUS_ACTIVE
    assert resumed['next_in'] == published_gbfs_endpoint.interval


def test_message_from_before_a_pause_does_not_fork_the_resumed_loop(
    published_gbfs_endpoint, queued, fetches
):
    """pause-fetch clears the marker and resume-fetch seeds a new loop; the
    message queued before the pause must not keep a second loop going."""
    tasks.bootstrap_realtime_tasks()
    before_pause = queued()[-1][0]
    cache.delete(tasks._rt_alive_key(published_gbfs_endpoint.id))  # pause-fetch
    assert tasks.schedule_realtime_endpoint_fetches(published_gbfs_endpoint.submission_id) == 1
    after_resume = queued()[-1][0]

    assert _replay(before_pause)['status'] == 'stale'
    assert _replay(after_resume)['status'] == 'ok'
    assert fetches.calls == 1


def test_message_queued_before_loop_tokens_is_adopted_once(
    published_gbfs_endpoint, queued, fetches
):
    """Messages queued by the previous release carry no token and their marker
    is '1': the first one keeps the loop alive, a duplicate of it exits."""
    cache.set(tasks._rt_alive_key(published_gbfs_endpoint.id), tasks.RT_LEGACY_ALIVE_MARKER)

    assert _replay([published_gbfs_endpoint.id])['status'] == 'ok'
    assert _replay([published_gbfs_endpoint.id])['status'] == 'stale'
    assert fetches.calls == 1
    assert len(queued()) == 1


def _make_due(endpoint):
    RealtimeEndpointRT.objects.filter(pk=endpoint.pk).update(
        next_fetch_after=timezone.now() - timedelta(seconds=1)
    )


@pytest.mark.parametrize('failures_before, wait', [(0, 30), (1, 120)])
def test_short_backoff_step_is_queued_until_it_ends(
    published_gbfs_endpoint, queued, fetches, failures_before, wait
):
    """30 s and 2 min steps are shorter than the bootstrap period, so the loop
    queues them instead of leaving the endpoint idle for up to 5 minutes."""
    RealtimeEndpointRT.objects.filter(pk=published_gbfs_endpoint.pk).update(
        fetch_failure_count=failures_before
    )
    tasks.bootstrap_realtime_tasks()
    fetches.fail = True
    fetches.delays = REALTIME_TRANSIENT_RETRY_DELAYS

    result = _replay(queued()[-1][0])

    assert result['fetch_status'] == FETCH_STATUS_DELAYED
    (retry_args, countdown) = queued()[-1]
    assert wait - 1 <= countdown <= wait
    assert retry_args[1] == cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id))
    # The queued retry holds the loop: bootstrap does not seed a second one.
    _make_due(published_gbfs_endpoint)
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 0

    fetches.fail = False
    resumed = _replay(retry_args)
    assert resumed['fetch_status'] == FETCH_STATUS_ACTIVE
    assert resumed['next_in'] == published_gbfs_endpoint.interval


def test_backoff_step_of_bootstrap_period_or_more_waits_for_bootstrap(
    published_gbfs_endpoint, queued, fetches
):
    RealtimeEndpointRT.objects.filter(pk=published_gbfs_endpoint.pk).update(fetch_failure_count=2)
    tasks.bootstrap_realtime_tasks()
    fetches.fail = True
    fetches.delays = REALTIME_TRANSIENT_RETRY_DELAYS

    result = _replay(queued()[-1][0])

    # Third step, 10 minutes: no ETA, the loop stops until bootstrap resumes it.
    assert result['fetch_status'] == FETCH_STATUS_DELAYED
    assert len(queued()) == 1
    assert cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id)) is None


@pytest.mark.parametrize('fails', [False, True], ids=['success', 'failure'])
def test_auto_paused_endpoint_is_retried_daily_through_bootstrap(
    published_gbfs_endpoint, queued, fetches, fails
):
    RealtimeEndpointRT.objects.filter(pk=published_gbfs_endpoint.pk).update(
        fetch_status=FETCH_STATUS_AUTO_PAUSED,
        fetch_failure_count=len(REALTIME_TRANSIENT_RETRY_DELAYS) + 1,
        next_fetch_after=timezone.now() + timedelta(hours=1),
    )
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 0

    _make_due(published_gbfs_endpoint)
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 1
    fetches.fail = fails
    fetches.delays = REALTIME_TRANSIENT_RETRY_DELAYS
    result = _replay(queued()[-1][0])

    assert fetches.calls == 1
    if fails:
        assert result['fetch_status'] == FETCH_STATUS_AUTO_PAUSED
        assert result['fetch_failure_count'] == len(REALTIME_TRANSIENT_RETRY_DELAYS) + 2
        assert (
            timedelta(hours=23) < result['next_fetch_after'] - timezone.now() <= timedelta(days=1)
        )
        # The daily retry is never queued as an ETA.
        assert len(queued()) == 1
        assert cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id)) is None
    else:
        assert result['fetch_status'] == FETCH_STATUS_ACTIVE
        assert result['next_in'] == published_gbfs_endpoint.interval
        assert len(queued()) == 2


def test_bootstrap_period_matches_the_beat_schedule(settings):
    """Steps shorter than RT_BOOTSTRAP_PERIOD_SECONDS are queued on the
    assumption that bootstrap runs that often."""
    schedule = settings.CELERY_BEAT_SCHEDULE['bootstrap-realtime-tasks']['schedule']
    assert schedule == crontab(minute=f'*/{tasks.RT_BOOTSTRAP_PERIOD_SECONDS // 60}')
