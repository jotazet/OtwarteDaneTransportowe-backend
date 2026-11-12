"""Graph build pipeline: per-feed isolation, failure policies, activation and recovery."""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from planner import pipeline, services
from planner.models import GraphBuild, PlannerFeed
from planner.tests.conftest import GTFS_ZIP, gtfs_variant, set_feed_content

pytestmark = pytest.mark.django_db


def build_now(trigger=GraphBuild.TRIGGER_MANUAL) -> GraphBuild:
    build = services.request_build(trigger)
    pipeline.run_build(build.id)
    build.refresh_from_db()
    return build


def results(build) -> dict:
    return {r['feed_id']: r for r in build.feed_results}


def serving_target() -> str:
    return os.readlink(pipeline.data_path('serving'))


def test_first_build_tests_every_feed_and_activates(make_feed, fake_otp):
    make_feed('krk')
    make_feed('waw')

    build = build_now()

    assert build.status == GraphBuild.STATUS_SUCCEEDED
    assert build.is_active
    assert fake_otp.kinds() == ['test', 'test', 'street', 'graph']
    assert fake_otp.events == ['stop', 'start']
    assert serving_target() == f'graphs/{build.id}'
    assert {r['outcome'] for r in build.feed_results} == {'fresh'}
    for feed in PlannerFeed.objects.all():
        assert feed.last_good_sha256
        assert Path(pipeline.snapshot_path(feed)).read_bytes() == GTFS_ZIP


def test_build_containers_get_memory_limits_and_the_street_graph_read_only(
    make_feed, fake_otp, settings
):
    make_feed('krk')

    build_now()

    test_run, street_run, graph_run = fake_otp.runs
    assert test_run['heap'] == settings.PLANNER_OTP_FEED_TEST_HEAP
    assert graph_run['memswap_limit'] == settings.PLANNER_OTP_BUILD_MEMSWAP_LIMIT
    [(host, container)] = graph_run['mounts'].items()
    assert host.endswith('streetGraph.obj')
    assert container == '/var/opentripplanner/streetGraph.obj'


def test_unchanged_feed_and_area_are_not_rebuilt(make_feed, fake_otp):
    make_feed('krk')
    build_now()
    fake_otp.runs.clear()

    build = build_now()

    assert build.status == GraphBuild.STATUS_SUCCEEDED
    assert fake_otp.kinds() == ['graph']


def test_new_feed_version_that_fails_its_test_falls_back_to_last_good(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    good_sha = PlannerFeed.objects.get(pk=feed.pk).last_good_sha256
    set_feed_content(feed, gtfs_variant('broken'))
    fake_otp.failing_feeds['krk'] = (255, 'CsvEntityIOException: stop_times.txt line 7')

    build = build_now()

    result = results(build)['krk']
    assert build.status == GraphBuild.STATUS_SUCCEEDED
    assert result['outcome'] == 'stale'
    assert result['error_code'] == 'planner.feed_invalid'
    assert 'stop_times.txt' in result['log_excerpt']
    assert result['sha256'] == good_sha
    assert PlannerFeed.objects.get(pk=feed.pk).last_good_sha256 == good_sha
    assert build.id != first.id and build.is_active


def test_exclude_policy_drops_a_failing_feed(make_feed, fake_otp):
    make_feed('krk')
    feed = make_feed('waw', failure_policy=PlannerFeed.POLICY_EXCLUDE)
    build_now()
    set_feed_content(feed, gtfs_variant('broken'))
    fake_otp.failing_feeds['waw'] = (255, 'boom')
    fake_otp.runs.clear()

    build = build_now()

    assert results(build)['waw']['outcome'] == 'excluded'
    assert fake_otp.runs[-1]['feeds'] == ['krk']


def test_failing_feed_without_a_last_good_version_is_excluded(make_feed, fake_otp):
    make_feed('krk')
    make_feed('waw')
    fake_otp.failing_feeds['waw'] = (255, 'boom')

    build = build_now()

    assert results(build)['waw']['outcome'] == 'excluded'
    assert build.status == GraphBuild.STATUS_SUCCEEDED


def test_feed_without_service_in_the_window_gets_its_own_code(make_feed, fake_otp):
    make_feed('krk')
    make_feed('waw')
    fake_otp.failing_feeds['waw'] = (100, 'The provided transit data have no trips within ...')

    build = build_now()

    assert results(build)['waw']['error_code'] == 'planner.feed_no_service_in_period'


def test_unpublished_feed_is_excluded(make_feed, fake_otp):
    make_feed('krk')
    make_feed('waw', published=False)

    build = build_now()

    waw = results(build)['waw']
    assert (waw['outcome'], waw['error_code']) == ('excluded', 'planner.feed_not_published')


def test_corrupt_copy_is_treated_as_an_unavailable_source(make_feed, fake_otp):
    make_feed('krk')
    make_feed('waw', content=b'PK\x03\x04 truncated')

    build = build_now()

    waw = results(build)['waw']
    assert (waw['outcome'], waw['error_code']) == ('excluded', 'planner.feed_source_unavailable')


def test_failed_full_build_keeps_the_previous_graph_serving(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    fake_otp.graph_result = (1, 'boom')
    fake_otp.events.clear()

    build = build_now()

    assert (build.status, build.error_code) == (
        GraphBuild.STATUS_FAILED,
        'planner.graph_build_failed',
    )
    assert GraphBuild.objects.get(is_active=True) == first
    assert serving_target() == f'graphs/{first.id}'
    assert fake_otp.events == ['stop', 'start']


def test_out_of_memory_gets_its_own_code(make_feed, fake_otp):
    make_feed('krk')
    fake_otp.graph_result = (3, 'Terminating due to java.lang.OutOfMemoryError')

    build = build_now()

    assert build.error_code == 'planner.build_out_of_memory'


def test_unhealthy_new_graph_is_rolled_back(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    fake_otp.healthy = False

    build = build_now()

    assert build.error_code == 'planner.activation_failed'
    assert GraphBuild.objects.get(is_active=True) == first
    assert serving_target() == f'graphs/{first.id}'


def test_missing_osm_extract_fails_and_restarts_otp(make_feed, fake_otp, planner_data):
    make_feed('krk')
    (planner_data / 'osm' / 'region.osm.pbf').unlink()

    build = build_now()

    assert build.error_code == 'planner.osm_missing'
    assert fake_otp.running


def test_rollback_serves_an_earlier_graph(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    second = build_now()
    assert second.is_active
    first.refresh_from_db()

    rollback = services.request_rollback(first)
    pipeline.run_build(rollback.id)
    rollback.refresh_from_db()

    assert rollback.status == GraphBuild.STATUS_SUCCEEDED
    assert rollback.is_active
    assert serving_target() == f'graphs/{first.id}'


def test_second_build_request_is_refused_while_one_is_queued(make_feed):
    services.request_build(GraphBuild.TRIGGER_MANUAL)

    with pytest.raises(services.BuildInProgress):
        services.request_build(GraphBuild.TRIGGER_SCHEDULE)


def test_build_with_no_feeds_fails_without_stopping_otp(fake_otp, planner_data):
    build = build_now()

    assert build.error_code == 'planner.no_feeds'
    assert fake_otp.events == []


def test_interrupted_build_is_failed_and_the_active_graph_restarted(make_feed, fake_otp):
    make_feed('krk')
    active = build_now()
    stale = GraphBuild.objects.create(
        trigger=GraphBuild.TRIGGER_SCHEDULE,
        otp_image=active.otp_image,
        status=GraphBuild.STATUS_RUNNING,
        otp_stopped_at=timezone.now() - timedelta(hours=1),
        heartbeat_at=timezone.now() - timedelta(hours=1),
    )
    fake_otp.running = False

    assert pipeline.fail_stale_builds() == 1
    pipeline.ensure_serving()

    stale.refresh_from_db()
    assert (stale.status, stale.error_code) == ('failed', 'planner.build_interrupted')
    assert fake_otp.running
    assert serving_target() == f'graphs/{active.id}'


def test_ensure_serving_leaves_a_running_build_alone(make_feed, fake_otp):
    make_feed('krk')
    build_now()
    GraphBuild.objects.create(
        trigger=GraphBuild.TRIGGER_MANUAL,
        otp_image='x',
        status=GraphBuild.STATUS_RUNNING,
        heartbeat_at=timezone.now(),
    )
    fake_otp.running = False

    pipeline.ensure_serving()

    assert not fake_otp.running


def declare_interrupted():
    """What the watchdog does once a running build's heartbeat is 15 minutes old."""
    GraphBuild.objects.filter(status=GraphBuild.STATUS_RUNNING).update(
        heartbeat_at=timezone.now() - timedelta(hours=1)
    )
    assert pipeline.fail_stale_builds() == 1


def test_build_declared_interrupted_during_osmium_stops_without_activating(
    make_feed, fake_otp, planner_data, monkeypatch
):
    make_feed('krk')
    first = build_now()
    (planner_data / 'osm' / 'region.osm.pbf').write_bytes(b'new map')  # rebuild streets
    fake_otp.runs.clear()
    fake_otp.events.clear()
    beats = []

    def prepare_osm(source, cells, target, on_tick=None):
        running = GraphBuild.objects.filter(status=GraphBuild.STATUS_RUNNING)
        running.update(heartbeat_at=timezone.now() - timedelta(minutes=10))
        on_tick()  # osmium is running: the heartbeat is refreshed
        beats.append(running.get().heartbeat_at)
        declare_interrupted()
        on_tick()  # the next heartbeat finds the build failed

    monkeypatch.setattr(pipeline.osm, 'prepare_osm', prepare_osm)

    build = build_now()

    assert beats[0] > timezone.now() - timedelta(minutes=1)
    assert (build.status, build.error_code) == ('failed', 'planner.build_interrupted')
    assert 'street' not in fake_otp.kinds() and 'graph' not in fake_otp.kinds()
    # Serving is the watchdog's again: the aborted build does not start OTP.
    assert fake_otp.events == ['stop']
    assert GraphBuild.objects.get(is_active=True) == first
    assert serving_target() == f'graphs/{first.id}'


def test_build_declared_interrupted_during_activation_never_becomes_active(
    make_feed, fake_otp, monkeypatch
):
    feed = make_feed('krk')
    first = build_now()
    feed.refresh_from_db()
    last_good = feed.last_good_sha256
    set_feed_content(feed, gtfs_variant('v2'))
    fake_otp.events.clear()
    real_query = fake_otp.query

    def query(*args, **kwargs):  # the activation check, after the health wait
        declare_interrupted()
        return real_query(*args, **kwargs)

    monkeypatch.setattr(pipeline.otp_runtime, 'query', query)

    build = build_now()

    assert (build.status, build.error_code) == ('failed', 'planner.build_interrupted')
    assert not build.is_active
    assert GraphBuild.objects.get(is_active=True) == first
    assert serving_target() == f'graphs/{first.id}'
    assert fake_otp.events == ['stop', 'start', 'stop']
    assert not os.path.exists(pipeline.graph_dir(build))
    assert os.path.exists(pipeline.graph_dir(first))
    feed.refresh_from_db()
    assert feed.last_good_sha256 == last_good


def test_build_failing_after_it_was_declared_interrupted_leaves_serving_alone(
    make_feed, fake_otp, monkeypatch
):
    make_feed('krk')
    real_run_otp = fake_otp.run_otp

    def run_otp(host_dir, args, **kwargs):
        if args == ['--loadStreet', '--save']:
            # ensure_serving removed the container before the build's next heartbeat.
            declare_interrupted()
            return pipeline.otp_runtime.RunResult(exit_code=137, oom=False, log_tail='')
        return real_run_otp(host_dir, args, **kwargs)

    monkeypatch.setattr(pipeline.otp_runtime, 'run_otp', run_otp)

    build = build_now()

    assert (build.status, build.error_code) == ('failed', 'planner.build_interrupted')
    assert fake_otp.events == ['stop']


def test_watchdog_does_not_overwrite_a_finished_build(make_feed, fake_otp):
    make_feed('krk')
    build = build_now()
    GraphBuild.objects.filter(pk=build.pk).update(heartbeat_at=timezone.now() - timedelta(hours=1))

    assert pipeline.fail_stale_builds() == 0
    build.refresh_from_db()
    assert build.status == GraphBuild.STATUS_SUCCEEDED


def test_heartbeat_is_refreshed_while_feeds_are_collected(make_feed, fake_otp, monkeypatch):
    make_feed('krk')
    make_feed('waw')
    beats = []
    real_heartbeat = pipeline._heartbeat
    monkeypatch.setattr(
        pipeline,
        '_heartbeat',
        lambda build: (beats.append(list(fake_otp.events)), real_heartbeat(build)),
    )

    build_now()

    assert len([events for events in beats if 'stop' not in events]) >= 2


def test_unreachable_docker_fails_the_build_with_its_own_code(make_feed, fake_otp, monkeypatch):
    make_feed('krk')

    def run_otp(*args, **kwargs):
        raise pipeline.otp_runtime.DockerUnavailable('connection refused')

    monkeypatch.setattr(pipeline.otp_runtime, 'run_otp', run_otp)

    build = build_now()

    assert build.error_code == 'planner.docker_unavailable'
    assert fake_otp.running


def test_snapshot_of_a_deleted_feed_is_not_reused(make_feed, fake_otp, planner_data):
    feed = make_feed('waw')
    snapshot = Path(pipeline.snapshot_path(feed))
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(GTFS_ZIP)  # left behind by an earlier 'waw' that was deleted
    make_feed('krk')
    fake_otp.failing_feeds['waw'] = (255, 'boom')

    build = build_now()

    assert results(build)['waw']['outcome'] == 'excluded'


def test_failed_first_activation_serves_nothing(make_feed, fake_otp):
    make_feed('krk')
    fake_otp.healthy = False

    build = build_now()

    assert build.error_code == 'planner.activation_failed'
    assert not os.path.lexists(pipeline.data_path('serving'))


class WorkerKilled(BaseException):
    """A deploy or OOM kill: no except clause of the pipeline runs."""


def test_ensure_serving_restarts_otp_left_on_a_graph_that_never_became_active(
    make_feed, fake_otp, monkeypatch
):
    feed = make_feed('krk')
    active = build_now()
    set_feed_content(feed, gtfs_variant('v2'))

    def killed(build):
        raise WorkerKilled

    monkeypatch.setattr(pipeline, '_wait_until_healthy', killed)
    with pytest.raises(WorkerKilled):
        build_now()
    interrupted = GraphBuild.objects.latest('id')
    assert serving_target() == f'graphs/{interrupted.id}'
    assert fake_otp.running  # on the graph the database says is not active
    declare_interrupted()
    fake_otp.events.clear()

    pipeline.ensure_serving()

    assert fake_otp.events == ['stop', 'start']
    assert serving_target() == f'graphs/{active.id}'
    assert not os.path.exists(pipeline.graph_dir(interrupted))
    assert os.path.exists(pipeline.graph_dir(active))


def test_ensure_serving_restarts_otp_when_serving_is_not_a_symlink(make_feed, fake_otp):
    make_feed('krk')
    active = build_now()
    serving = Path(pipeline.data_path('serving'))
    serving.unlink()
    serving.mkdir()  # what Docker creates before the first build
    fake_otp.events.clear()

    pipeline.ensure_serving()

    assert fake_otp.events == ['stop', 'start']
    assert serving_target() == f'graphs/{active.id}'


def test_failed_activation_removes_its_graph(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    fake_otp.healthy = False

    build = build_now()

    assert build.error_code == 'planner.activation_failed'
    assert not os.path.exists(pipeline.graph_dir(build))
    assert os.path.exists(pipeline.graph_dir(first))


def test_failed_rollback_keeps_the_graph_it_rolled_back_to(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    second = build_now()
    first.refresh_from_db()
    fake_otp.healthy = False

    rollback = services.request_rollback(first)
    pipeline.run_build(rollback.id)
    rollback.refresh_from_db()

    assert rollback.error_code == 'planner.activation_failed'
    assert serving_target() == f'graphs/{second.id}'
    assert os.path.exists(pipeline.graph_dir(first))


def test_ensure_serving_removes_only_files_of_finished_builds(make_feed, fake_otp, planner_data):
    feed = make_feed('krk')
    rollback_target = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    active = build_now()
    failed = GraphBuild.objects.create(
        trigger=GraphBuild.TRIGGER_MANUAL, otp_image='x', status=GraphBuild.STATUS_FAILED
    )
    queued = GraphBuild.objects.create(trigger=GraphBuild.TRIGGER_MANUAL, otp_image='x')
    leftovers = [
        planner_data / 'builds' / str(failed.id) / 'feeds',
        planner_data / 'graphs' / str(failed.id),
    ]
    untouched = [
        planner_data / 'builds' / str(queued.id),
        planner_data / 'graphs' / str(queued.id),
        planner_data / 'graphs' / '99999',  # no such build
        planner_data / 'graphs' / 'notes',
        planner_data / 'graphs' / '\u00b2',  # isdigit(), but not an id
    ]
    for path in leftovers + untouched:
        path.mkdir(parents=True)
    fake_otp.events.clear()

    pipeline.ensure_serving()

    assert fake_otp.events == []
    assert not (planner_data / 'builds' / str(failed.id)).exists()
    assert not leftovers[1].exists()
    assert all(path.exists() for path in untouched)
    assert os.path.exists(pipeline.graph_dir(active))
    assert os.path.exists(pipeline.graph_dir(rollback_target))


def test_ensure_serving_keeps_a_rolled_back_graph_and_the_one_before(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    second = build_now()
    first.refresh_from_db()
    rollback = services.request_rollback(first)
    pipeline.run_build(rollback.id)
    fake_otp.events.clear()

    pipeline.ensure_serving()

    assert fake_otp.events == []
    assert serving_target() == f'graphs/{first.id}'
    assert os.path.exists(pipeline.graph_dir(first))
    assert os.path.exists(pipeline.graph_dir(second))


def test_feed_copy_is_retried_on_the_current_file(make_feed, fake_otp, monkeypatch):
    feed = make_feed('krk', content=b'PK\x03\x04 mid-rewrite')
    beats = []
    real_heartbeat = pipeline._heartbeat
    monkeypatch.setattr(
        pipeline, '_heartbeat', lambda build: (beats.append(1), real_heartbeat(build))
    )

    def retry(seconds):
        beats.append('retry')
        set_feed_content(feed, GTFS_ZIP)

    monkeypatch.setattr(pipeline, '_sleep', retry)

    build = build_now()

    assert results(build)['krk']['outcome'] == 'fresh'
    # One heartbeat for the feed, one more before the retry (a download may take
    # the whole fetch timeout).
    assert beats[: beats.index('retry')] == [1, 1]


def test_last_good_version_is_retested_after_an_otp_upgrade(
    make_feed, fake_otp, settings, monkeypatch
):
    make_feed('krk')
    waw = make_feed('waw')
    build_now()
    settings.PLANNER_OTP_IMAGE = 'opentripplanner/opentripplanner:2.11.0'
    set_feed_content(waw, b'not a zip')  # newest version unusable -> last good version
    fake_otp.failing_feeds['waw'] = (100, 'incompatible')

    build = build_now()

    waw_result = results(build)['waw']
    assert (waw_result['outcome'], waw_result['error_code']) == ('excluded', 'planner.feed_invalid')
    assert build.status == GraphBuild.STATUS_SUCCEEDED


def test_excluded_feed_keeps_its_area_in_the_street_graph(make_feed, fake_otp):
    make_feed('krk')
    waw = make_feed('waw', content=gtfs_variant('v1'), failure_policy=PlannerFeed.POLICY_EXCLUDE)
    build_now()
    set_feed_content(waw, gtfs_variant('v2'))
    fake_otp.failing_feeds['waw'] = (255, 'boom')
    fake_otp.runs.clear()

    build = build_now()

    assert results(build)['waw']['outcome'] == 'excluded'
    assert 'street' not in fake_otp.kinds()


def test_build_after_a_rollback_keeps_the_graph_rolled_back_to(make_feed, fake_otp):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    second = build_now()
    first.refresh_from_db()
    rollback = services.request_rollback(first)
    pipeline.run_build(rollback.id)
    set_feed_content(feed, gtfs_variant('v3'))

    third = build_now()

    assert third.is_active
    assert os.path.exists(pipeline.graph_dir(third))
    assert os.path.exists(pipeline.graph_dir(first))  # served before the switch
    assert not os.path.exists(pipeline.graph_dir(second))


def test_failed_build_keeps_maintenance_until_otp_is_healthy_again(
    make_feed, fake_otp, monkeypatch
):
    feed = make_feed('krk')
    first = build_now()
    set_feed_content(feed, gtfs_variant('v2'))
    fake_otp.graph_result = (1, 'boom')
    monkeypatch.setattr(pipeline, 'SERVER_START_TIMEOUT_SECONDS', 3600)
    seen = []

    def is_healthy():
        seen.append((fake_otp.running, services.maintenance_active()))
        return len(seen) > 2  # OTP loads the previous graph for a while

    monkeypatch.setattr(pipeline.otp_runtime, 'is_healthy', is_healthy)

    build = build_now()

    assert build.error_code == 'planner.graph_build_failed'
    assert seen == [(True, True)] * 3
    assert not services.maintenance_active()
    assert serving_target() == f'graphs/{first.id}'


def test_health_wait_stops_once_the_container_has_exited(make_feed, fake_otp, monkeypatch):
    build = GraphBuild.objects.create(
        trigger=GraphBuild.TRIGGER_MANUAL,
        otp_image='x',
        status=GraphBuild.STATUS_RUNNING,
        heartbeat_at=timezone.now(),
    )
    monkeypatch.setattr(pipeline, 'SERVER_START_TIMEOUT_SECONDS', 3600)
    sleeps = []
    monkeypatch.setattr(pipeline, '_sleep', sleeps.append)
    fake_otp.healthy = False
    fake_otp.running = False

    assert pipeline._wait_until_healthy(build) is False
    assert sleeps == []


def test_snapshot_left_behind_by_a_crash_is_replaced_at_the_next_build(
    make_feed, fake_otp, planner_data
):
    feed = make_feed('krk')
    build_now()
    feed.refresh_from_db()
    snapshot = Path(pipeline.snapshot_path(feed))
    good = snapshot.read_bytes()
    # A crash after the commit: the database has the new hash, the file is the old one.
    snapshot.write_bytes(gtfs_variant('old'))
    Path(f'{snapshot}.tmp').write_bytes(good)

    build_now()

    assert snapshot.read_bytes() == good
    assert not Path(f'{snapshot}.tmp').exists()
