"""Ad-hoc N+1 verification: query count for the feed-submissions list must not grow with N."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)


@pytest.mark.django_db
def test_list_query_count_constant(django_assert_max_num_queries):
    User = get_user_model()
    admin_group, _ = Group.objects.get_or_create(name='Admin')
    admin = User.objects.create_user(username='adm', password='x')
    admin.groups.add(admin_group)

    org = TransportOrganization.objects.create(region='R', transport_organization='Org')
    for i in range(20):
        sub = FeedSubmission.objects.create(
            transport_organization=org, data_type='gtfs', name=f'f{i}', submitted_by=admin
        )
        for stage in (2, 3, 4):
            FeedSubmissionHistory.objects.create(
                submission=sub,
                event_type='stage_advanced',
                stage_before=stage - 1,
                stage_after=stage,
                actor=admin,
            )

    client = APIClient()
    client.force_authenticate(user=admin)
    # Generous fixed budget: must not scale with the 20 submissions
    # (pre-fix this endpoint issued 100+ queries for 20 rows).
    with django_assert_max_num_queries(20):
        resp = client.get('/api/data_manager/feed-submissions/')
    assert resp.status_code == 200
    rows = resp.data['results']
    assert resp.data['count'] == 20
    assert all(row['current_stage'] == 4 for row in rows)


# ---------------------------------------------------------------------------
# Query counts that must not grow with the number of rows (issue #50)
# ---------------------------------------------------------------------------


def _query_count(client, url):
    # completed_submission_ids() is cached; clear it so each call sees new rows.
    cache.clear()
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200
    return len(ctx), response.data


def _published_feed_with_realtime(org, user):
    """A published static submission with a published GTFS-RT submission attached."""
    sub = FeedSubmission.objects.create(
        transport_organization=org, data_type='gtfs', name='f', submitted_by=user
    )
    FeedSubmissionHistory.objects.create(
        submission=sub,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=user,
    )
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=user,
        protocol=RealtimeSubmission.PROTOCOL_GTFS_RT,
        static_submission=sub,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=user,
    )
    RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='trip_update',
        url='https://example.org/tu.pb',
    )
    return sub


def _make_org_with_feed(i, user):
    org = TransportOrganization.objects.create(region='R', transport_organization=f'Org {i}')
    _published_feed_with_realtime(org, user)
    return org


def test_public_feeds_list_query_count_constant(make_user):
    admin = make_user('Admin')
    client = APIClient()
    _make_org_with_feed(0, admin)
    one, _ = _query_count(client, '/api/data_manager/feeds/')
    for i in range(1, 5):
        _make_org_with_feed(i, admin)
    many, data = _query_count(client, '/api/data_manager/feeds/')

    assert data['count'] == 5
    assert all(row['dynamic_types'] == ['gtfs-rt'] for row in data['results'])
    assert one == many == 10


# The lists show no realtime submissions, so they must not prefetch them.
@pytest.mark.parametrize(
    ('url', 'queries'),
    [('/api/data_manager/submissions/', 5), ('/api/data_manager/feed-submissions/', 3)],
)
def test_submission_lists_query_count_constant(make_user, client_as, url, queries):
    admin = make_user('Admin')
    client = client_as(admin)
    org = TransportOrganization.objects.create(region='R', transport_organization='Org')
    _published_feed_with_realtime(org, admin)
    client.get(url)  # warm-up: the first request caches the user's roles
    one, _ = _query_count(client, url)
    for _ in range(4):
        _published_feed_with_realtime(org, admin)
    many, _ = _query_count(client, url)

    assert one == many == queries


def test_feed_submission_detail_still_embeds_realtime(make_user, client_as):
    admin = make_user('Admin')
    org = TransportOrganization.objects.create(region='R', transport_organization='Org')
    sub = _published_feed_with_realtime(org, admin)

    data = client_as(admin).get(f'/api/data_manager/feed-submissions/{sub.pk}/').data

    [rts] = data['realtime_submissions']
    assert [e['endpoint_type'] for e in rts['endpoints']] == ['trip_update']
    assert rts['current_stage'] == 4
