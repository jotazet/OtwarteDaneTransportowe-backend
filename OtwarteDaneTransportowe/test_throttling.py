"""Scoped DRF throttles identify clients by the trusted-proxy client IP."""

import pytest
from django.utils.module_loading import import_string
from rest_framework.test import APIClient

from OtwarteDaneTransportowe import settings_base

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


def _anon_list(client, **headers):
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
        _anon_list(client, **_via_proxy('198.51.100.1', '6.6.6.6')).status_code for _ in range(5)
    }

    assert statuses == {200}
