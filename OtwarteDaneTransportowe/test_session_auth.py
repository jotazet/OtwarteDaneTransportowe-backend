"""Cookie sessions of the website (ADR 0017, docs/api/README.md#cookie-sessions-for-the-website)."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from OtwarteDaneTransportowe.session_auth import REFRESH_COOKIE, SESSION_PATH

pytestmark = pytest.mark.django_db

ORIGIN = 'https://odt.example.org'
BROWSER = {'HTTP_ORIGIN': ORIGIN, 'HTTP_X_ODT_CSRF': '1'}


@pytest.fixture(autouse=True)
def _frontend_origin(settings):
    settings.CORS_ALLOWED_ORIGINS = [ORIGIN]
    settings.CORS_ALLOWED_ORIGIN_REGEXES = []


@pytest.fixture
def user():
    return get_user_model().objects.create_user('alice', password='alice-pass-123')


def _login(client, password='alice-pass-123', **headers):
    return client.post(
        SESSION_PATH, {'username': 'alice', 'password': password}, format='json', **headers
    )


def _is_blacklisted(token):
    with pytest.raises(Exception, match='blacklisted'):
        RefreshToken(token)
    return True


def test_login_sets_the_refresh_cookie_and_returns_only_the_access_token(user):
    response = _login(APIClient(), **BROWSER)

    assert response.status_code == 200
    assert set(response.data) == {'access'}
    assert AccessToken(response.data['access'])['user_id'] == str(user.pk)
    cookie = response.cookies[REFRESH_COOKIE]
    assert RefreshToken(cookie.value)['user_id'] == str(user.pk)
    assert cookie['httponly'] is True
    assert cookie['secure'] is True
    assert cookie['samesite'] == 'Strict'
    assert cookie['path'] == SESSION_PATH
    assert cookie['max-age'] == 24 * 3600
    assert not cookie['domain']


def test_login_with_wrong_password_is_a_code(user):
    response = _login(APIClient(), password='wrong', **BROWSER)

    assert response.status_code == 401
    assert response.data == {'code': 'auth.invalid_credentials', 'details': {}}
    assert REFRESH_COOKIE not in response.cookies


def test_refresh_rotates_the_cookie_and_blacklists_the_old_token(user):
    client = APIClient()
    old = _login(client, **BROWSER).cookies[REFRESH_COOKIE].value

    response = client.post(f'{SESSION_PATH}refresh/', **BROWSER)

    assert response.status_code == 200
    assert set(response.data) == {'access'}
    new = response.cookies[REFRESH_COOKIE]
    assert new.value != old
    assert new['httponly'] is True
    assert new['path'] == SESSION_PATH
    assert RefreshToken(new.value)['user_id'] == str(user.pk)
    assert _is_blacklisted(old)
    # The client now holds the new cookie, so the next refresh works too.
    assert client.post(f'{SESSION_PATH}refresh/', **BROWSER).status_code == 200


def test_refresh_without_cookie_is_a_code():
    response = APIClient().post(f'{SESSION_PATH}refresh/', **BROWSER)

    assert response.status_code == 401
    assert response.data == {'code': 'auth.refresh_cookie_missing', 'details': {}}


@pytest.mark.parametrize('value', ['garbage', 'blacklisted'])
def test_refresh_with_bad_cookie_is_a_code_and_keeps_the_cookie(user, value):
    if value == 'blacklisted':
        token = RefreshToken.for_user(user)
        token.blacklist()
        value = str(token)
    client = APIClient()
    client.cookies[REFRESH_COOKIE] = value

    response = client.post(f'{SESSION_PATH}refresh/', **BROWSER)

    assert response.status_code == 401
    assert response.data == {'code': 'auth.refresh_cookie_invalid', 'details': {}}
    assert REFRESH_COOKIE not in response.cookies


@pytest.mark.parametrize(
    'headers',
    [
        {},
        {'HTTP_ORIGIN': ORIGIN},
        {'HTTP_X_ODT_CSRF': '1'},
        {'HTTP_ORIGIN': 'https://evil.example', 'HTTP_X_ODT_CSRF': '1'},
        {'HTTP_ORIGIN': 'null', 'HTTP_X_ODT_CSRF': '1'},
        {'HTTP_ORIGIN': ORIGIN, 'HTTP_X_ODT_CSRF': 'yes'},
    ],
)
@pytest.mark.parametrize('path', ['', 'refresh/', 'logout/'])
def test_csrf_check_refuses_requests_without_header_or_allowed_origin(user, headers, path):
    client = APIClient()
    token = str(RefreshToken.for_user(user))
    client.cookies[REFRESH_COOKIE] = token

    response = client.post(
        f'{SESSION_PATH}{path}',
        {'username': 'alice', 'password': 'alice-pass-123'},
        format='json',
        **headers,
    )

    assert response.status_code == 403
    assert response.data == {'code': 'auth.csrf_failed', 'details': {}}
    assert REFRESH_COOKIE not in response.cookies
    RefreshToken(token)  # still valid: not rotated, not blacklisted


def test_logout_clears_the_cookie_and_blacklists_the_token(user):
    client = APIClient()
    token = _login(client, **BROWSER).cookies[REFRESH_COOKIE].value

    response = client.post(f'{SESSION_PATH}logout/', **BROWSER)

    assert response.status_code == 200
    assert response.data == {}
    cleared = response.cookies[REFRESH_COOKIE]
    assert cleared.value == ''
    assert cleared['max-age'] == 0
    assert cleared['path'] == SESSION_PATH
    assert _is_blacklisted(token)
    refresh = client.post(f'{SESSION_PATH}refresh/', **BROWSER)
    assert refresh.data['code'] == 'auth.refresh_cookie_missing'


@pytest.mark.parametrize('cookie', [None, 'garbage'])
def test_logout_without_a_valid_cookie_still_succeeds(cookie):
    client = APIClient()
    if cookie:
        client.cookies[REFRESH_COOKIE] = cookie

    response = client.post(f'{SESSION_PATH}logout/', **BROWSER)

    assert response.status_code == 200
    assert response.cookies[REFRESH_COOKIE]['max-age'] == 0


def test_session_endpoints_ignore_an_admin_session_cookie(user):
    """A Django admin session sent along must not trigger DRF's session CSRF check."""
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)

    assert _login(client, **BROWSER).status_code == 200


def test_secure_flag_can_be_turned_off_for_local_http(settings, user):
    settings.AUTH_REFRESH_COOKIE_SECURE = False

    cookie = _login(APIClient(), **BROWSER).cookies[REFRESH_COOKIE]

    assert cookie['secure'] == ''


def test_cors_allows_credentials_on_session_endpoints_for_allowed_origins_only(user):
    client = APIClient()
    preflight = client.options(
        f'{SESSION_PATH}refresh/',
        HTTP_ORIGIN=ORIGIN,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type, x-odt-csrf',
    )
    assert preflight['Access-Control-Allow-Origin'] == ORIGIN
    assert preflight['Access-Control-Allow-Credentials'] == 'true'
    assert 'x-odt-csrf' in preflight['Access-Control-Allow-Headers']

    login = _login(client, **BROWSER)
    assert login['Access-Control-Allow-Credentials'] == 'true'

    foreign = client.options(
        f'{SESSION_PATH}refresh/',
        HTTP_ORIGIN='https://evil.example',
        HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
    )
    assert 'Access-Control-Allow-Origin' not in foreign
    assert 'Access-Control-Allow-Credentials' not in foreign


@pytest.mark.parametrize('path', ['/api/auth/token/', '/api/blog/posts/', '/api/planner/v1/plan/'])
def test_cors_does_not_allow_credentials_elsewhere(path):
    response = APIClient().options(
        path, HTTP_ORIGIN=ORIGIN, HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST'
    )

    assert response['Access-Control-Allow-Origin'] == ORIGIN
    assert 'Access-Control-Allow-Credentials' not in response


def test_cors_exposes_content_disposition():
    response = APIClient().get('/api/blog/posts/', HTTP_ORIGIN=ORIGIN)

    assert 'Content-Disposition' in response['Access-Control-Expose-Headers']


def test_body_based_endpoints_are_unchanged(user):
    """Other API clients keep /api/auth/token/ with the refresh token in the body."""
    client = APIClient()
    pair = client.post(
        '/api/auth/token/', {'username': 'alice', 'password': 'alice-pass-123'}, format='json'
    )
    assert pair.status_code == 200
    assert set(pair.data) == {'access', 'refresh'}
    assert REFRESH_COOKIE not in pair.cookies

    rotated = client.post(
        '/api/auth/token/refresh/', {'refresh': pair.data['refresh']}, format='json'
    )
    assert rotated.status_code == 200
    assert set(rotated.data) == {'access', 'refresh'}

    assert (
        client.post(
            '/api/auth/logout/', {'refresh': rotated.data['refresh']}, format='json'
        ).status_code
        == 200
    )
