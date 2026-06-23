import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from OtwarteDaneTransportowe.auth_roles import ROLE_ADMIN, ROLE_DATA_PROVIDER

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def admin_group():
    group, _ = Group.objects.get_or_create(name=ROLE_ADMIN)
    return group


@pytest.fixture
def admin_user(admin_group):
    user = User.objects.create_user('admin_api', 'admin@example.com', 'adminpass')
    user.groups.add(admin_group)
    return user


@pytest.fixture
def regular_user():
    user = User.objects.create_user('regular', 'regular@example.com', 'regularpass')
    group, _ = Group.objects.get_or_create(name=ROLE_DATA_PROVIDER)
    user.groups.add(group)
    return user


def test_list_users_requires_admin_group(api_client, admin_user, regular_user):
    api_client.force_authenticate(user=regular_user)
    response = api_client.get('/api/users/')
    assert response.status_code == 403

    api_client.force_authenticate(user=admin_user)
    response = api_client.get('/api/users/')
    assert response.status_code == 200
    usernames = {u['username'] for u in response.data}
    assert 'admin_api' in usernames
    assert 'regular' in usernames


def test_create_user_returns_generated_password(api_client, admin_user):
    api_client.force_authenticate(user=admin_user)
    response = api_client.post(
        '/api/users/',
        {
            'username': 'newuser',
            'email': 'new@example.com',
            'first_name': 'New',
            'last_name': 'User',
            'roles': [ROLE_DATA_PROVIDER],
        },
        format='json',
    )
    assert response.status_code == 201, response.data
    assert 'generated_password' in response.data
    assert len(response.data['generated_password']) >= 16

    user = User.objects.get(username='newuser')
    assert user.check_password(response.data['generated_password'])
    assert ROLE_DATA_PROVIDER in response.data['roles']

    login = api_client.post(
        '/api/auth/token/',
        {'username': 'newuser', 'password': response.data['generated_password']},
        format='json',
    )
    assert login.status_code == 200


def test_reset_password(api_client, admin_user, regular_user):
    api_client.force_authenticate(user=admin_user)
    response = api_client.post(f'/api/users/{regular_user.pk}/reset-password/')
    assert response.status_code == 200
    assert 'generated_password' in response.data

    regular_user.refresh_from_db()
    assert regular_user.check_password(response.data['generated_password'])


def test_change_password(api_client, regular_user):
    api_client.force_authenticate(user=regular_user)
    response = api_client.post(
        '/api/users/me/change-password/',
        {'current_password': 'regularpass', 'new_password': 'NewSecurePass9!'},
        format='json',
    )
    assert response.status_code == 200

    regular_user.refresh_from_db()
    assert regular_user.check_password('NewSecurePass9!')


def test_change_email(api_client, regular_user):
    api_client.force_authenticate(user=regular_user)
    response = api_client.post(
        '/api/users/me/change-email/',
        {'current_password': 'regularpass', 'new_email': 'changed@example.com'},
        format='json',
    )
    assert response.status_code == 200
    assert response.data['email'] == 'changed@example.com'


def test_me_endpoint(api_client, regular_user):
    api_client.force_authenticate(user=regular_user)
    response = api_client.get('/api/users/me/')
    assert response.status_code == 200
    assert response.data['username'] == 'regular'


def test_cannot_delete_self(api_client, admin_user):
    api_client.force_authenticate(user=admin_user)
    response = api_client.delete(f'/api/users/{admin_user.pk}/')
    assert response.status_code == 400


def test_cannot_remove_last_admin(api_client, admin_user):
    api_client.force_authenticate(user=admin_user)
    response = api_client.patch(
        f'/api/users/{admin_user.pk}/',
        {'roles': [ROLE_DATA_PROVIDER]},
        format='json',
    )
    assert response.status_code == 400

    response = api_client.delete(f'/api/users/{admin_user.pk}/')
    assert response.status_code == 400


def test_update_user_roles(api_client, admin_user, regular_user):
    api_client.force_authenticate(user=admin_user)
    response = api_client.patch(
        f'/api/users/{regular_user.pk}/',
        {'roles': [ROLE_ADMIN, ROLE_DATA_PROVIDER]},
        format='json',
    )
    assert response.status_code == 200
    assert set(response.data['roles']) == {ROLE_ADMIN, ROLE_DATA_PROVIDER}
