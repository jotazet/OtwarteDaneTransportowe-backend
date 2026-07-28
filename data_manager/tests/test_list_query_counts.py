"""Ad-hoc N+1 verification: query count for the feed-submissions list must not grow with N."""
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, FeedSubmissionHistory


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
                submission=sub, event_type='stage_advanced',
                stage_before=stage - 1, stage_after=stage, actor=admin,
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
