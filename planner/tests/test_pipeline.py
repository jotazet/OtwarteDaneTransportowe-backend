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


def test_feed_copy_is_retried_on_the_current_file(make_feed, fake_otp, monkeypatch):
    feed = make_feed('krk', content=b'PK\x03\x04 mid-rewrite')
    monkeypatch.setattr(pipeline, '_sleep', lambda seconds: set_feed_content(feed, GTFS_ZIP))

    build = build_now()

    assert results(build)['krk']['outcome'] == 'fresh'


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
    first = build_now()
    set_feed_content(waw, gtfs_variant('v2'))
    fake_otp.failing_feeds['waw'] = (255, 'boom')
    fake_otp.runs.clear()

    build = build_now()

    assert results(build)['waw']['outcome'] == 'excluded'
    assert 'street' not in fake_otp.kinds()
    assert build.street_graph_key == first.street_graph_key
