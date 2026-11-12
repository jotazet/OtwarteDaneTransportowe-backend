"""Non-numeric ids in URLs and integer query filters: 404 or a coded 400, never a 500."""

import pytest

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission
from OtwarteDaneTransportowe.auth_roles import ROLE_DATA_PROVIDER

pytestmark = pytest.mark.django_db


@pytest.fixture
def org():
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def submission(org, make_user):
    return FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=make_user(ROLE_DATA_PROVIDER),
        data_type='gtfs',
        name='Feed',
    )


@pytest.fixture
def client(submission, client_as):
    return client_as(submission.submitted_by)


@pytest.mark.parametrize(
    'path',
    [
        '/api/data_manager/submissions/',
        '/api/data_manager/feed-submissions/',
        '/api/data_manager/realtime-submissions/',
    ],
)
def test_transport_organization_filter_rejects_non_integer(client, path):
    response = client.get(path, {'transport_organization': 'abc'})

    assert response.status_code == 400
    assert response.json() == {'transport_organization': ['feeds.invalid_integer']}


def test_transport_organization_filter_still_filters(client, submission, org):
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
def test_non_numeric_id_in_url_is_404(client, submission, path):
    response = client.get(path.format(pk=submission.pk))

    assert response.status_code == 404
