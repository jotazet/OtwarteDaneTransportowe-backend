from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from accounts.tasks import flush_expired_tokens_task
from OtwarteDaneTransportowe.auth_roles import ROLE_ADMIN, ROLE_DATA_PROVIDER
from OtwarteDaneTransportowe.jwt_views import RolesInTokenObtainPairSerializer

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
    usernames = {u['username'] for u in response.data['results']}
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


def _session(user):
    """A refresh token of a new session of ``user``, as issued at login."""
    return str(RolesInTokenObtainPairSerializer.get_token(user))


def _refresh(refresh):
    return APIClient().post('/api/auth/token/refresh/', {'refresh': refresh}, format='json')


def _logout(refresh):
    return APIClient().post('/api/auth/logout/', {'refresh': refresh}, format='json')


def _change_password(client, current_password):
    return client.post(
        '/api/users/me/change-password/',
        {'current_password': current_password, 'new_password': 'NewSecurePass9!'},
        format='json',
    )


def test_reset_password_ends_the_users_sessions(api_client, admin_user, regular_user):
    sessions = [_session(regular_user) for _ in range(2)]
    admin_session = _session(admin_user)
    api_client.force_authenticate(user=admin_user)

    assert api_client.post(f'/api/users/{regular_user.pk}/reset-password/').status_code == 200

    assert [_refresh(r).status_code for r in sessions] == [401, 401]
    assert _refresh(admin_session).status_code == 200


def test_change_password_ends_other_sessions_and_returns_a_new_pair(api_client, regular_user):
    old_session = _session(regular_user)
    api_client.force_authenticate(user=regular_user)

    response = _change_password(api_client, 'regularpass')

    assert response.status_code == 200
    regular_user.refresh_from_db()
    assert regular_user.check_password('NewSecurePass9!')
    assert _refresh(old_session).status_code == 401
    assert _refresh(response.data['refresh']).status_code == 200
    me = APIClient()
    me.credentials(HTTP_AUTHORIZATION=f'Bearer {response.data["access"]}')
    assert me.get('/api/users/me/').status_code == 200


def test_wrong_current_password_keeps_the_sessions(api_client, regular_user):
    session = _session(regular_user)
    api_client.force_authenticate(user=regular_user)

    assert _change_password(api_client, 'wrong').status_code == 400
    assert _refresh(session).status_code == 200


def test_logout_blacklists_the_refresh_token(make_user):
    user = make_user(ROLE_DATA_PROVIDER)
    session, other_session = _session(user), _session(user)

    assert _logout(session).status_code == 200
    assert _refresh(session).status_code == 401
    assert _refresh(other_session).status_code == 200
    # A second logout with the same token: 401, which the client treats as done.
    assert _logout(session).status_code == 401


def test_flush_expired_tokens_task_deletes_only_expired_tokens(make_user):
    user = make_user(ROLE_DATA_PROVIDER)
    _session(user)
    expired = OutstandingToken.objects.create(
        user=user, jti='expired', token='x', expires_at=timezone.now() - timedelta(days=1)
    )

    flush_expired_tokens_task()

    assert not OutstandingToken.objects.filter(pk=expired.pk).exists()
    assert OutstandingToken.objects.filter(user=user).count() == 1


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


def test_cannot_deactivate_last_admin(api_client, admin_user):
    api_client.force_authenticate(user=admin_user)
    response = api_client.patch(
        f'/api/users/{admin_user.pk}/',
        {'is_active': False},
        format='json',
    )
    assert response.status_code == 400
    admin_user.refresh_from_db()
    assert admin_user.is_active

    # Keeping the Admin role in the same payload must not bypass the guard:
    # a deactivated admin no longer counts, whatever roles remain.
    response = api_client.patch(
        f'/api/users/{admin_user.pk}/',
        {'roles': [ROLE_ADMIN], 'is_active': False},
        format='json',
    )
    assert response.status_code == 400


def test_can_deactivate_admin_when_another_remains(api_client, admin_user, admin_group):
    second = User.objects.create_user('admin2', 'admin2@example.com', 'pass')
    second.groups.add(admin_group)

    api_client.force_authenticate(user=admin_user)
    response = api_client.patch(
        f'/api/users/{second.pk}/',
        {'is_active': False},
        format='json',
    )
    assert response.status_code == 200
    second.refresh_from_db()
    assert not second.is_active

    # Reactivating the (now sole active) admin's colleague is fine, and a
    # plain reactivation payload never trips the guard.
    response = api_client.patch(
        f'/api/users/{second.pk}/',
        {'is_active': True},
        format='json',
    )
    assert response.status_code == 200
