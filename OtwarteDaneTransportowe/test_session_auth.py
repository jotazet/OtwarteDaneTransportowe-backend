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
@pytest.mark.parametrize('path', ['', 'refresh/', 'logout/', 'change-password/'])
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


def test_refresh_has_its_own_budget_that_forged_requests_do_not_use(monkeypatch, user):
    from rest_framework.throttling import SimpleRateThrottle
    from rest_framework.views import APIView

    from OtwarteDaneTransportowe.throttling import ScopedRateThrottle

    monkeypatch.setattr(APIView, 'throttle_classes', [ScopedRateThrottle])
    rates = {'login': '1/min', 'session_refresh': '2/min'}
    monkeypatch.setattr(SimpleRateThrottle, 'THROTTLE_RATES', rates)
    client = APIClient()
    assert _login(client, **BROWSER).status_code == 200  # uses up the login budget

    forged = [client.post(f'{SESSION_PATH}refresh/').status_code for _ in range(3)]
    refreshes = [client.post(f'{SESSION_PATH}refresh/', **BROWSER).status_code for _ in range(3)]

    assert forged == [403, 403, 403]
    assert refreshes == [200, 200, 429]


NEW_PASSWORD = 'NewSecurePass9!'


def _change_password(client, access, current='alice-pass-123', **headers):
    if access:
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
    return client.post(
        f'{SESSION_PATH}change-password/',
        {'current_password': current, 'new_password': NEW_PASSWORD},
        format='json',
        **headers,
    )


def test_change_password_sets_a_new_cookie_and_ends_all_other_sessions(user):
    client = APIClient()
    login = _login(client, **BROWSER)
    old = login.cookies[REFRESH_COOKIE].value
    other_device = _login(APIClient(), **BROWSER).cookies[REFRESH_COOKIE].value

    response = _change_password(client, login.data['access'], **BROWSER)

    assert response.status_code == 200
    assert set(response.data) == {'access'}
    assert AccessToken(response.data['access'])['user_id'] == str(user.pk)
    cookie = response.cookies[REFRESH_COOKIE]
    assert cookie['httponly'] is True
    assert cookie['path'] == SESSION_PATH
    assert cookie['max-age'] == 24 * 3600
    assert RefreshToken(cookie.value)['user_id'] == str(user.pk)
    assert _is_blacklisted(old)
    assert _is_blacklisted(other_device)
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    # The client now holds the new cookie, so the session continues.
    client.credentials()
    assert client.post(f'{SESSION_PATH}refresh/', **BROWSER).status_code == 200


def test_change_password_with_wrong_current_password_keeps_the_session(user):
    client = APIClient()
    login = _login(client, **BROWSER)

    response = _change_password(client, login.data['access'], current='wrong', **BROWSER)

    # The same answer as /api/users/me/change-password/.
    assert response.status_code == 400
    assert response.data == {'current_password': ['users.wrong_password']}
    assert REFRESH_COOKIE not in response.cookies
    RefreshToken(login.cookies[REFRESH_COOKIE].value)  # still valid
    user.refresh_from_db()
    assert user.check_password('alice-pass-123')


def test_change_password_checks_csrf_before_the_access_token(user):
    access = _login(APIClient(), **BROWSER).data['access']

    response = _change_password(APIClient(), access, HTTP_ORIGIN=ORIGIN)

    assert response.status_code == 403
    assert response.data == {'code': 'auth.csrf_failed', 'details': {}}
    user.refresh_from_db()
    assert user.check_password('alice-pass-123')


@pytest.mark.parametrize('credentials', ['none', 'invalid_bearer', 'admin_session'])
def test_change_password_requires_the_access_token(user, credentials):
    client = APIClient(enforce_csrf_checks=True)
    access = None
    if credentials == 'invalid_bearer':
        access = 'garbage'
    elif credentials == 'admin_session':
        client.force_login(user)  # a Django session cookie is not accepted here

    response = _change_password(client, access, **BROWSER)

    assert response.status_code == 401
    assert REFRESH_COOKIE not in response.cookies
    user.refresh_from_db()
    assert user.check_password('alice-pass-123')


def test_change_password_shares_the_account_change_budget(monkeypatch, user):
    from rest_framework.throttling import SimpleRateThrottle
    from rest_framework.views import APIView

    from OtwarteDaneTransportowe.throttling import ScopedRateThrottle

    monkeypatch.setattr(APIView, 'throttle_classes', [ScopedRateThrottle])
    rates = {'login': '10/min', 'account_change': '2/min'}
    monkeypatch.setattr(SimpleRateThrottle, 'THROTTLE_RATES', rates)
    client = APIClient()
    access = _login(client, **BROWSER).data['access']

    forged = _change_password(client, access, current='wrong').status_code
    body_based = client.post(
        '/api/users/me/change-password/',
        {'current_password': 'wrong', 'new_password': NEW_PASSWORD},
        format='json',
    ).status_code
    session = [
        _change_password(client, access, current='wrong', **BROWSER).status_code for _ in range(2)
    ]

    assert forged == 403
    assert [body_based, *session] == [400, 400, 429]
