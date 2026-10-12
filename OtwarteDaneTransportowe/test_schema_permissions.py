"""Outside DEBUG the API schema is for the Admin role only, not for is_staff."""

import importlib

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import clear_url_caches
from rest_framework.test import APIClient

from OtwarteDaneTransportowe.auth_roles import ROLE_ADMIN

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def production_urlconf(settings):
    """Rebuild the URLconf with DEBUG off: urls.py picks the schema permissions at import."""
    import OtwarteDaneTransportowe.urls as urls

    debug = settings.DEBUG
    settings.DEBUG = False
    importlib.reload(urls)
    clear_url_caches()
    yield
    settings.DEBUG = debug
    importlib.reload(urls)
    clear_url_caches()


def _client(*, staff=False, groups=()):
    user = User.objects.create_user('schema-user', 'schema@example.org', 'password')
    user.is_staff = staff
    user.save(update_fields=['is_staff'])
    for name in groups:
        user.groups.add(Group.objects.get_or_create(name=name)[0])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_anonymous_cannot_read_schema(production_urlconf):
    assert APIClient().get('/api/schema/swagger-ui/').status_code == 401


def test_staff_without_admin_role_cannot_read_schema(production_urlconf):
    assert _client(staff=True).get('/api/schema/swagger-ui/').status_code == 403


def test_admin_role_can_read_schema(production_urlconf):
    assert _client(groups=[ROLE_ADMIN]).get('/api/schema/swagger-ui/').status_code == 200
