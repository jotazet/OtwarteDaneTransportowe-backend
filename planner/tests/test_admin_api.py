"""Admin API of the planner: roles, feed selection, builds, schedule and status."""

import pytest
from django_celery_beat.models import PeriodicTask

from planner import otp_runtime
from planner.models import GraphBuild, PlannerFeed

pytestmark = pytest.mark.django_db

BASE = '/api/planner/admin'


@pytest.fixture
def admin_client(make_user, client_as):
    return client_as(make_user('Admin'))


@pytest.mark.parametrize(
    ('role', 'expected'),
    [
        (None, 401),
        ('DataProvider', 403),
        ('Helper', 403),
        ('Blogger', 403),
        ('Editor', 403),
        ('Admin', 200),
    ],
)
@pytest.mark.parametrize(
    'path', ['/feeds/', '/feed-candidates/', '/builds/', '/schedule/', '/status/']
)
def test_planner_admin_requires_the_admin_group(
    make_user, client_as, monkeypatch, role, expected, path
):
    monkeypatch.setattr(otp_runtime, 'is_healthy', lambda: True)

    response = client_as(make_user(role)).get(BASE + path)

    assert response.status_code == expected


def test_superuser_without_the_admin_group_is_refused(django_user_model, client_as):
    superuser = django_user_model.objects.create_superuser('root', 'root@example.org', 'x')

    assert client_as(superuser).get(f'{BASE}/feeds/').status_code == 403


def test_admin_adds_a_published_gtfs_feed(admin_client, make_submission):
    submission = make_submission('krk')

    response = admin_client.post(
        f'{BASE}/feeds/', {'submission': submission.id, 'feed_id': 'krk'}, format='json'
    )

    assert response.status_code == 201
    assert response.data['organization']['name'] == 'Org krk'
    assert response.data['failure_policy'] == PlannerFeed.POLICY_KEEP_LAST_GOOD


@pytest.mark.parametrize(
    ('kwargs', 'code'),
    [
        ({'published': False}, 'planner.feed_not_published'),
        ({'data_type': 'netex'}, 'planner.feed_not_gtfs'),
    ],
)
def test_only_published_gtfs_feeds_can_be_added(admin_client, make_submission, kwargs, code):
    submission = make_submission('krk', **kwargs)

    response = admin_client.post(
        f'{BASE}/feeds/', {'submission': submission.id, 'feed_id': 'krk'}, format='json'
    )

    assert response.status_code == 400
    assert response.data['submission'][0].code == code


def test_feed_id_is_validated_and_immutable(admin_client, make_feed, make_submission):
    bad = admin_client.post(
        f'{BASE}/feeds/',
        {'submission': make_submission('waw').id, 'feed_id': 'Bad:ID'},
        format='json',
    )
    feed = make_feed('krk')
    renamed = admin_client.patch(f'{BASE}/feeds/{feed.id}/', {'feed_id': 'other'}, format='json')

    assert bad.data['feed_id'][0].code == 'planner.invalid_feed_id'
    assert renamed.data['feed_id'][0].code == 'planner.feed_id_immutable'


def test_feed_moves_only_within_its_organization(admin_client, make_feed, make_submission):
    feed = make_feed('krk')
    same_org = make_submission('krk-2', org=feed.submission.transport_organization)
    other_org = make_submission('waw')

    moved = admin_client.patch(
        f'{BASE}/feeds/{feed.id}/', {'submission': same_org.id}, format='json'
    )
    refused = admin_client.patch(
        f'{BASE}/feeds/{feed.id}/', {'submission': other_org.id}, format='json'
    )

    assert moved.status_code == 200
    assert refused.data['submission'][0].code == 'planner.feed_other_organization'


def test_realtime_from_only_names_a_feed_of_the_same_organization(
    admin_client, make_feed, make_submission
):
    feed = make_feed('olkusz-shaped')
    same_org = make_submission('olkusz', org=feed.submission.transport_organization)
    other_org = make_submission('waw')

    linked = admin_client.patch(
        f'{BASE}/feeds/{feed.id}/', {'realtime_from': same_org.id}, format='json'
    )
    refused = admin_client.patch(
        f'{BASE}/feeds/{feed.id}/', {'realtime_from': other_org.id}, format='json'
    )
    cleared = admin_client.patch(f'{BASE}/feeds/{feed.id}/', {'realtime_from': None}, format='json')

    assert linked.status_code == 200
    assert linked.data['realtime_from'] == same_org.id
    assert refused.data['realtime_from'][0].code == 'planner.feed_other_organization'
    assert cleared.data['realtime_from'] is None


def test_candidates_show_which_feeds_are_already_included(admin_client, make_feed, make_submission):
    feed = make_feed('krk')
    make_submission('waw')
    make_submission('hidden', published=False)

    response = admin_client.get(f'{BASE}/feed-candidates/')

    by_name = {row['name']: row for row in response.data['results']}
    assert set(by_name) == {'krk', 'waw'}
    assert by_name['krk']['planner_feed'] == feed.id
    assert by_name['waw']['planner_feed'] is None


def test_build_now_is_queued_after_commit_and_a_second_one_refused(
    admin_client, celery_send_task, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        first = admin_client.post(f'{BASE}/builds/')
    second = admin_client.post(f'{BASE}/builds/')

    assert first.status_code == 202
    assert first.data['trigger'] == GraphBuild.TRIGGER_MANUAL
    assert celery_send_task.call_args.args[0] == 'planner.run_graph_build'
    assert second.status_code == 409
    assert second.data == {'code': 'planner.build_in_progress', 'details': {}}


def test_build_detail_shows_per_feed_results(admin_client):
    build = GraphBuild.objects.create(
        trigger=GraphBuild.TRIGGER_MANUAL,
        otp_image='img',
        status=GraphBuild.STATUS_FAILED,
        feed_results=[{'feed_id': 'krk', 'outcome': 'stale'}],
        error_code='planner.graph_build_failed',
    )

    listed = admin_client.get(f'{BASE}/builds/')
    detail = admin_client.get(f'{BASE}/builds/{build.id}/')

    assert 'feed_results' not in listed.data['results'][0]
    assert detail.data['feed_results'] == [{'feed_id': 'krk', 'outcome': 'stale'}]


def test_build_whose_graph_is_gone_cannot_be_activated(admin_client, planner_data):
    build = GraphBuild.objects.create(
        trigger=GraphBuild.TRIGGER_MANUAL, otp_image='img', status=GraphBuild.STATUS_SUCCEEDED
    )

    response = admin_client.post(f'{BASE}/builds/{build.id}/activate/')

    assert response.status_code == 409
    assert response.data['code'] == 'planner.build_not_activatable'


def test_schedule_defaults_until_saved(admin_client):
    response = admin_client.get(f'{BASE}/schedule/')

    assert response.data == {'enabled': False, 'weekdays': [3, 7], 'time_utc': '01:40'}


def test_schedule_is_stored_as_a_utc_crontab_that_does_not_fire_at_once(admin_client):
    response = admin_client.put(
        f'{BASE}/schedule/',
        {'enabled': True, 'weekdays': [7, 3], 'time_utc': '01:40'},
        format='json',
    )

    task = PeriodicTask.objects.get(name='planner-graph-build')
    assert response.data == {'enabled': True, 'weekdays': [3, 7], 'time_utc': '01:40'}
    assert task.task == 'planner.scheduled_graph_build'
    assert (task.crontab.day_of_week, task.crontab.hour, task.crontab.minute) == ('0,3', '1', '40')
    assert str(task.crontab.timezone) == 'UTC'
    assert task.last_run_at is None


@pytest.mark.parametrize(
    'payload',
    [
        {'enabled': True, 'weekdays': [], 'time_utc': '01:40'},
        {'enabled': True, 'weekdays': [8], 'time_utc': '01:40'},
        {'enabled': True, 'weekdays': [1], 'time_utc': '24:00'},
    ],
)
def test_invalid_schedule_is_refused(admin_client, payload):
    assert admin_client.put(f'{BASE}/schedule/', payload, format='json').status_code == 400


def test_status_reports_the_maintenance_window(admin_client, monkeypatch):
    monkeypatch.setattr(otp_runtime, 'is_healthy', lambda: False)
    GraphBuild.objects.create(
        trigger=GraphBuild.TRIGGER_SCHEDULE,
        otp_image='img',
        status=GraphBuild.STATUS_RUNNING,
        otp_stopped_at='2026-10-14T01:41:00Z',
    )

    response = admin_client.get(f'{BASE}/status/')

    assert response.data['maintenance'] is True
    assert response.data['otp_healthy'] is False
    assert response.data['running_build']['status'] == 'running'
    assert response.data['active_build'] is None


def test_feed_list_query_count_does_not_grow_with_feeds(
    admin_client, make_feed, django_assert_max_num_queries
):
    for index in range(10):
        make_feed(f'feed-{index}')

    with django_assert_max_num_queries(8):
        response = admin_client.get(f'{BASE}/feeds/')

    assert response.data['count'] == 10


def test_schedule_edited_elsewhere_still_reads(admin_client):
    from django_celery_beat.models import CrontabSchedule

    crontab = CrontabSchedule.objects.create(minute='0', hour='*', day_of_week='*', timezone='UTC')
    PeriodicTask.objects.create(
        name='planner-graph-build', task='planner.scheduled_graph_build', crontab=crontab
    )

    response = admin_client.get(f'{BASE}/schedule/')

    assert response.status_code == 200
    assert response.data['weekdays'] == [3, 7]
