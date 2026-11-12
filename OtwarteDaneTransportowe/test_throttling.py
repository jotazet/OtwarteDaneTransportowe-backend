"""Scoped DRF throttles identify clients by the trusted-proxy client IP."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils.module_loading import import_string
from rest_framework.test import APIClient

from blog.models import Post
from OtwarteDaneTransportowe import settings_base
from OtwarteDaneTransportowe.auth_roles import ROLE_DATA_PROVIDER

pytestmark = pytest.mark.django_db

PROXY = '10.0.0.5'


@pytest.fixture
def rates(monkeypatch, settings):
    """Re-enable the configured throttles; tests lower a scope in the returned rates.

    DRF copies the throttle classes and rates onto APIView and SimpleRateThrottle
    when they are first imported, so they are patched rather than set through
    REST_FRAMEWORK. They are imported here, under _disable_throttling: imported
    while tests are collected, they would keep the real throttles for the session.
    """
    from rest_framework.throttling import SimpleRateThrottle
    from rest_framework.views import APIView

    settings.TRUSTED_PROXY_CIDRS = ['10.0.0.0/8']
    classes = settings_base.REST_FRAMEWORK['DEFAULT_THROTTLE_CLASSES']
    monkeypatch.setattr(APIView, 'throttle_classes', [import_string(c) for c in classes])
    rates = dict.fromkeys(settings_base.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'], '1000/min')
    monkeypatch.setattr(SimpleRateThrottle, 'THROTTLE_RATES', rates)
    return rates


def _via_proxy(client_ip, spoofed):
    # What nginx's $proxy_add_x_forwarded_for forwards: the client's own
    # header with the real peer address appended.
    return {'REMOTE_ADDR': PROXY, 'HTTP_X_FORWARDED_FOR': f'{spoofed}, {client_ip}'}


def _list_posts(client, **headers):
    return client.get('/api/blog/posts/', **headers)


def _login(client, **headers):
    return client.post('/api/auth/token/', {'username': 'nobody', 'password': 'wrong'}, **headers)


def test_login_limit_follows_the_client_ip_not_x_forwarded_for(rates):
    rates['login'] = '2/min'
    client = APIClient()

    statuses = [
        _login(client, **_via_proxy('198.51.100.1', f'6.6.6.{n}')).status_code for n in range(3)
    ]

    assert statuses == [401, 401, 429]
    # Another client behind the same proxy keeps its own limit.
    assert _login(client, **_via_proxy('198.51.100.2', '6.6.6.0')).status_code == 401


def test_public_endpoints_without_a_scope_are_not_limited(rates):
    # Server-side rendering of the website calls them for all its visitors.
    rates.update(dict.fromkeys(rates, '1/min'))
    client = APIClient()

    statuses = {
        _list_posts(client, **_via_proxy('198.51.100.1', '6.6.6.6')).status_code for _ in range(5)
    }

    assert statuses == {200}


def test_reaction_changes_are_limited_per_client_ip(rates):
    # The daily reaction limit counts only new reactions, not changes.
    author = get_user_model().objects.create_user('author', 'author@example.com', 'password')
    post = Post.objects.create(title='Post', author=author, content='body')
    url = f'/api/blog/reactions/{post.id}/'
    rates['reaction'] = '2/min'
    client = APIClient()

    def react(client_ip, reaction):
        return client.post(url, {'reaction': reaction}, **_via_proxy(client_ip, '6.6.6.6'))

    statuses = [react('198.51.100.1', r).status_code for r in ('like', '', 'like')]

    assert 429 not in statuses[:2]
    assert statuses[2] == 429
    assert react('198.51.100.2', 'like').status_code != 429


def _admin_login(client, **headers):
    return client.post('/admin/login/', {'username': 'nobody', 'password': 'wrong'}, **headers)


def test_admin_login_is_limited_per_client_ip(rates):
    rates['login'] = '2/min'
    client = Client()

    statuses = [
        _admin_login(client, **_via_proxy('198.51.100.1', f'6.6.6.{n}')).status_code
        for n in range(3)
    ]

    # A failed admin login renders the form again with 200.
    assert statuses == [200, 200, 429]
    # Another client behind the same proxy keeps its own limit.
    assert _admin_login(client, **_via_proxy('198.51.100.2', '6.6.6.0')).status_code == 200


def test_admin_login_form_is_not_limited(rates):
    rates['login'] = '1/min'
    client = Client()
    headers = _via_proxy('198.51.100.1', '6.6.6.6')

    statuses = [client.get('/admin/login/', **headers).status_code for _ in range(2)]

    assert statuses == [200, 200]


def test_admin_login_posts_failing_csrf_are_not_counted(rates):
    rates['login'] = '1/min'
    headers = _via_proxy('198.51.100.1', '6.6.6.6')

    statuses = [
        _admin_login(Client(enforce_csrf_checks=True), **headers).status_code for _ in range(2)
    ]

    assert statuses == [403, 403]
    assert _admin_login(Client(), **headers).status_code == 200


def test_admin_and_token_logins_share_one_budget(rates):
    rates['login'] = '2/min'
    headers = _via_proxy('198.51.100.1', '6.6.6.6')

    assert _admin_login(Client(), **headers).status_code == 200
    assert _login(APIClient(), **headers).status_code == 401
    throttled = _admin_login(Client(), **headers)
    assert throttled.status_code == 429
    assert int(throttled['Retry-After']) > 0
    assert _login(APIClient(), **headers).status_code == 429


def test_admin_login_with_valid_credentials_still_signs_in(rates):
    get_user_model().objects.create_user('staff', 'staff@example.com', 'secret', is_staff=True)
    client = Client()

    # The admin form sends next=/admin/ as a hidden field.
    response = client.post(
        '/admin/login/',
        {'username': 'staff', 'password': 'secret', 'next': '/admin/'},
        **_via_proxy('198.51.100.1', '6.6.6.6'),
    )

    assert response.status_code == 302
    assert response.url == '/admin/'
    assert client.get('/admin/').status_code == 200


@pytest.mark.parametrize(
    ('path', 'body'),
    [
        ('/api/users/me/change-password/', {'current_password': 'wrong', 'new_password': 'x'}),
        ('/api/users/me/change-email/', {'current_password': 'wrong', 'new_email': 'a@b.cz'}),
    ],
)
def test_current_password_checks_are_limited_per_user(rates, make_user, client_as, path, body):
    rates['account_change'] = '2/min'
    client = client_as(make_user(ROLE_DATA_PROVIDER))

    statuses = [client.post(path, body, format='json').status_code for _ in range(3)]

    assert statuses == [400, 400, 429]
    # Another user from the same client IP keeps their own limit.
    other = client_as(make_user(ROLE_DATA_PROVIDER))
    assert other.post(path, body, format='json').status_code == 400
