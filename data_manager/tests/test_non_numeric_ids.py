"""Non-numeric ids in URLs and integer query filters: 404 or a coded 400, never a 500."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission

pytestmark = pytest.mark.django_db

INVALID = {'transport_organization': ['feeds.invalid_integer']}


@pytest.fixture
def org():
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def client_and_submission(org):
    user = get_user_model().objects.create_user('nn-provider', 'nn@example.com', 'password')
    user.groups.add(Group.objects.get_or_create(name='DataProvider')[0])
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=user, data_type='gtfs', name='Feed'
    )
    client = APIClient()
    client.force_authenticate(user)
    return client, submission


@pytest.mark.parametrize(
    'path',
    [
        '/api/data_manager/submissions/',
        '/api/data_manager/feed-submissions/',
        '/api/data_manager/realtime-submissions/',
    ],
)
def test_transport_organization_filter_rejects_non_integer(client_and_submission, path):
    client, _ = client_and_submission

    response = client.get(path, {'transport_organization': 'abc'})

    assert response.status_code == 400
    assert response.json() == INVALID


def test_transport_organization_filter_still_filters(client_and_submission, org):
    client, submission = client_and_submission
    other = TransportOrganization.objects.create(region='R', transport_organization='Other')

    own = client.get('/api/data_manager/feed-submissions/', {'transport_organization': org.pk})
    none = client.get('/api/data_manager/feed-submissions/', {'transport_organization': other.pk})
    empty = client.get('/api/data_manager/feed-submissions/', {'transport_organization': ''})
    huge = client.get('/api/data_manager/feed-submissions/', {'transport_organization': '9' * 30})

    assert [row['id'] for row in own.json()['results']] == [submission.pk]
    assert none.json()['results'] == []
    assert empty.json()['count'] == 1
    assert huge.json()['results'] == []  # out of the column's range: no match, not a 500


@pytest.mark.parametrize(
    'path',
    [
        '/api/data_manager/realtime-submissions/eligible-static-submissions/abc/',
        '/api/data_manager/realtime-submissions/eligible-static-submissions/abc/gtfs/',
        '/api/data_manager/feed-submissions/{pk}/download/static/abc/',
        '/api/data_manager/feed-submissions/{pk}/download/realtime/abc/',
        '/api/data_manager/feed-submissions/abc/',
        '/api/data_manager/realtime-submissions/abc/',
    ],
)
def test_non_numeric_id_in_url_is_404(client_and_submission, path):
    client, submission = client_and_submission

    response = client.get(path.format(pk=submission.pk))

    assert response.status_code == 404
