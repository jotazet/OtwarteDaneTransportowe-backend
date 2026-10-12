"""Cases write permissions per role (docs/api/cases.md).

POST/PATCH/DELETE on data providers, case statuses and transport organizations
need the case manager role: Helper or Admin. Everyone else is refused.
"""

import pytest

from cases.models import CaseStatus, DataProvider, TransportOrganization
from OtwarteDaneTransportowe.auth_roles import (
    ROLE_ADMIN,
    ROLE_BLOGGER,
    ROLE_DATA_PROVIDER,
    ROLE_EDITOR,
    ROLE_HELPER,
)

pytestmark = pytest.mark.django_db

ANONYMOUS = None

# (role, may write)
ROLES = [
    (ANONYMOUS, False),
    (ROLE_DATA_PROVIDER, False),
    (ROLE_BLOGGER, False),
    (ROLE_EDITOR, False),
    (ROLE_HELPER, True),
    (ROLE_ADMIN, True),
]

# resource -> (model, create payload, existing object, field and value changed by PATCH)
RESOURCES = {
    'data-providers': (
        DataProvider,
        lambda org: {'name': 'New provider'},
        lambda org: DataProvider.objects.create(name='Existing provider'),
        ('name', 'Renamed provider'),
    ),
    'case-statuses': (
        CaseStatus,
        lambda org: {'case': org.id, 'status': 'requested', 'description': 'Request sent'},
        # TransportOrganization.save() creates the initial 'none' status.
        lambda org: org.case_status.get(),
        ('status', 'denial'),
    ),
    'transport-organizations': (
        TransportOrganization,
        lambda org: {'region': 'Other region', 'transport_organization': 'New org'},
        lambda org: org,
        ('website', 'https://example.org/'),
    ),
}


def _expected(role, allowed, success):
    if allowed:
        return success
    return 401 if role is ANONYMOUS else 403


@pytest.fixture
def org():
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.mark.parametrize('resource', RESOURCES)
@pytest.mark.parametrize('role, allowed', ROLES)
def test_create_by_role(resource, role, allowed, org, make_user, client_as):
    model, payload, _, _ = RESOURCES[resource]
    before = model.objects.count()
    client = client_as(make_user(role))

    response = client.post(f'/api/cases/{resource}/', payload(org), format='json')

    assert response.status_code == _expected(role, allowed, 201), response.data
    assert model.objects.count() == before + (1 if allowed else 0)


@pytest.mark.parametrize('resource', RESOURCES)
@pytest.mark.parametrize('role, allowed', ROLES)
def test_update_by_role(resource, role, allowed, org, make_user, client_as):
    _, _, existing, (field, value) = RESOURCES[resource]
    obj = existing(org)
    original = getattr(obj, field)
    client = client_as(make_user(role))

    response = client.patch(f'/api/cases/{resource}/{obj.pk}/', {field: value})

    assert response.status_code == _expected(role, allowed, 200), response.data
    obj.refresh_from_db()
    assert getattr(obj, field) == (value if allowed else original)


@pytest.mark.parametrize('resource', RESOURCES)
@pytest.mark.parametrize('role, allowed', ROLES)
def test_delete_by_role(resource, role, allowed, org, make_user, client_as):
    model, _, existing, _ = RESOURCES[resource]
    obj = existing(org)

    response = client_as(make_user(role)).delete(f'/api/cases/{resource}/{obj.pk}/')

    assert response.status_code == _expected(role, allowed, 204)
    assert model.objects.filter(pk=obj.pk).exists() is not allowed


def test_created_organization_starts_with_status_none(make_user, client_as):
    response = client_as(make_user(ROLE_HELPER)).post(
        '/api/cases/transport-organizations/',
        {'region': 'R', 'transport_organization': 'New org'},
        format='json',
    )

    assert response.status_code == 201, response.data
    assert response.data['latest_status']['status'] == 'none'
