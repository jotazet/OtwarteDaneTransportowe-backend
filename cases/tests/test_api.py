"""Cases API: reads are intentionally public (transparency portal),
writes require the Helper/Admin (case manager) role."""
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from cases.models import CaseStatus, TransportOrganization

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def helper_user():
    user = get_user_model().objects.create_user('case-helper', 'ch@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='Helper')
    user.groups.add(group)
    return user


@pytest.fixture
def org():
    return TransportOrganization.objects.create(
        region='R', transport_organization='Org', is_public=False,
        contact_email='org@example.com',
    )


def test_anonymous_can_read_organizations_and_statuses(api_client, org):
    response = api_client.get('/api/cases/transport-organizations/')
    assert response.status_code == 200
    # is_public describes the organization type (public vs private company),
    # NOT visibility — private organizations are listed too.
    assert [row['id'] for row in response.data] == [org.id]

    # TransportOrganization.save() auto-creates an initial CaseStatus row.
    response = api_client.get('/api/cases/case-statuses/')
    assert response.status_code == 200
    assert len(response.data) == 1


def test_anonymous_cannot_write(api_client, org):
    response = api_client.post(
        '/api/cases/transport-organizations/',
        {'region': 'X', 'transport_organization': 'New'},
    )
    assert response.status_code in (401, 403)

    response = api_client.post(
        '/api/cases/case-statuses/',
        {'case': org.id, 'status': 'denial', 'description': 'nope'},
    )
    assert response.status_code in (401, 403)


def test_case_manager_can_write(api_client, helper_user, org):
    api_client.force_authenticate(user=helper_user)
    response = api_client.post(
        '/api/cases/case-statuses/',
        {'case': org.id, 'status': 'denial', 'description': 'refused'},
    )
    assert response.status_code == 201
    assert CaseStatus.objects.filter(case=org, status='denial').exists()
