"""Regression tests for the realtime self-scheduling loop.

Guards against the previous behaviour where the periodic ``bootstrap_realtime_tasks``
re-queued every active endpoint on each run, accumulating duplicate fetch chains.
"""

import pytest

from cases.models import TransportOrganization
from data_manager import tasks
from data_manager.models import (
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)

pytestmark = pytest.mark.django_db


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
    from django.core.cache import cache

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
    from django.core.cache import cache

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


@pytest.fixture
def queued(monkeypatch):
    """Record realtime fetch messages instead of queueing them: [(args, countdown)]."""
    calls = []
    monkeypatch.setattr(
        tasks.fetch_realtime_endpoint_task,
        'apply_async',
        lambda args=None, countdown=None, **kwargs: calls.append((args, countdown)),
    )
    return calls


@pytest.fixture
def fetches(monkeypatch):
    """Fake upstream fetch that succeeds unless ``fetches.fail`` is set."""

    def fake_fetch(endpoint, now):
        fake_fetch.calls += 1
        if fake_fetch.fail:
            endpoint.mark_fetch_failure('upstream down', now=now)
            return 'error'
        endpoint.mark_fetch_success(now=now)
        return 'ok'

    fake_fetch.calls = 0
    fake_fetch.fail = False
    monkeypatch.setattr('data_manager.scheduler._fetch_realtime_endpoint_rt', fake_fetch)
    return fake_fetch


def _run(args):
    return tasks.fetch_realtime_endpoint_task(*args)


def test_stale_token_exits_without_fetching_or_rescheduling(
    published_gbfs_endpoint, queued, fetches
):
    from django.core.cache import cache

    cache.set(tasks._rt_alive_key(published_gbfs_endpoint.id), 'current-token')

    result = _run([published_gbfs_endpoint.id, 'old-token'])

    assert result['status'] == 'stale'
    assert fetches.calls == 0
    assert queued == []
    assert cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id)) == 'current-token'


def test_each_run_hands_a_fresh_token_to_its_successor(published_gbfs_endpoint, queued, fetches):
    """A redelivered copy of an already executed message (Redis visibility
    timeout) carries the old token and must not fork the loop."""
    tasks.bootstrap_realtime_tasks()
    ((seed_args, _),) = queued

    first = _run(seed_args)
    assert first['next_in'] == published_gbfs_endpoint.interval
    next_args, countdown = queued[-1]
    assert next_args[0] == published_gbfs_endpoint.id
    assert next_args[1] != seed_args[1]
    assert countdown == published_gbfs_endpoint.interval

    assert _run(seed_args)['status'] == 'stale'
    assert fetches.calls == 1
    assert len(queued) == 2


def test_bootstrap_after_marker_expiry_leaves_one_effective_loop(
    published_gbfs_endpoint, queued, fetches
):
    """Scenario: the queue lags past the alive marker's TTL, bootstrap seeds a
    second loop, and the old loop's message still runs afterwards."""
    from django.core.cache import cache

    tasks.bootstrap_realtime_tasks()
    _run(queued[-1][0])
    old_loop_message = queued[-1][0]

    cache.delete(tasks._rt_alive_key(published_gbfs_endpoint.id))  # marker expired
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 1
    new_loop_message = queued[-1][0]

    assert _run(old_loop_message)['status'] == 'stale'
    assert _run(new_loop_message)['status'] == 'ok'
    assert fetches.calls == 2
    # Only the new loop rescheduled itself.
    assert len(queued) == 4
    assert queued[-1][0][1] == cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id))


def test_delayed_endpoint_is_not_queued_and_resumes_through_bootstrap(
    published_gbfs_endpoint, queued, fetches
):
    from datetime import timedelta

    from django.core.cache import cache
    from django.utils import timezone

    from data_manager.models import FETCH_STATUS_ACTIVE, FETCH_STATUS_DELAYED

    tasks.bootstrap_realtime_tasks()
    fetches.fail = True
    result = _run(queued[-1][0])

    # The back-off (5 min, 1 h, 6 h) is never queued as a long ETA.
    assert result['fetch_status'] == FETCH_STATUS_DELAYED
    assert len(queued) == 1
    assert cache.get(tasks._rt_alive_key(published_gbfs_endpoint.id)) is None

    # Not due yet: bootstrap leaves it alone.
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 0

    RealtimeEndpointRT.objects.filter(pk=published_gbfs_endpoint.pk).update(
        next_fetch_after=timezone.now() - timedelta(seconds=1)
    )
    assert tasks.bootstrap_realtime_tasks()['bootstrapped'] == 1
    fetches.fail = False
    resumed = _run(queued[-1][0])
    assert resumed['fetch_status'] == FETCH_STATUS_ACTIVE
    assert resumed['next_in'] == published_gbfs_endpoint.interval


def test_message_from_before_a_pause_does_not_fork_the_resumed_loop(
    published_gbfs_endpoint, queued, fetches
):
    """pause-fetch clears the marker and resume-fetch seeds a new loop; the
    message queued before the pause must not keep a second loop going."""
    from django.core.cache import cache

    tasks.bootstrap_realtime_tasks()
    before_pause = queued[-1][0]
    cache.delete(tasks._rt_alive_key(published_gbfs_endpoint.id))  # pause-fetch
    assert tasks.schedule_realtime_endpoint_fetches(published_gbfs_endpoint.submission_id) == 1
    after_resume = queued[-1][0]

    assert _run(before_pause)['status'] == 'stale'
    assert _run(after_resume)['status'] == 'ok'
    assert fetches.calls == 1


def test_message_queued_before_loop_tokens_is_adopted_once(
    published_gbfs_endpoint, queued, fetches
):
    """Messages queued by the previous release carry no token and their marker
    is '1': the first one keeps the loop alive, a duplicate of it exits."""
    from django.core.cache import cache

    cache.set(tasks._rt_alive_key(published_gbfs_endpoint.id), tasks.RT_LEGACY_ALIVE_MARKER)

    assert _run([published_gbfs_endpoint.id])['status'] == 'ok'
    assert _run([published_gbfs_endpoint.id])['status'] == 'stale'
    assert fetches.calls == 1
    assert len(queued) == 1
