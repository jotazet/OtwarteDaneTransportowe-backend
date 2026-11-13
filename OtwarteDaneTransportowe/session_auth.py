"""Browser sessions with the refresh token in an HttpOnly cookie (ADR 0017).

The website logs in, refreshes and logs out through ``/api/auth/session/``. The
refresh token travels only in the ``odt_refresh`` cookie, out of reach of the
page's scripts; responses carry the access token alone. The body-based token
endpoints (``/api/auth/token/``, ``/api/auth/logout/``) are unchanged for other
API clients.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from corsheaders.middleware import (
    ACCESS_CONTROL_ALLOW_CREDENTIALS,
    ACCESS_CONTROL_ALLOW_ORIGIN,
)
from corsheaders.middleware import CorsMiddleware as BaseCorsMiddleware
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from OtwarteDaneTransportowe.api_errors import CodedError
from OtwarteDaneTransportowe.jwt_views import RolesInTokenObtainPairSerializer

# urls.py builds the session routes from this prefix; the cookie's Path and the
# CORS credentials below depend on it.
SESSION_PATH = '/api/auth/session/'
REFRESH_COOKIE = 'odt_refresh'
CSRF_HEADER = 'X-ODT-CSRF'


def _origin_allowed(origin: str) -> bool:
    """Whether django-cors-headers' allowlist holds ``origin`` (one source of truth)."""
    try:
        url = urlsplit(origin)
    except ValueError:
        return False
    return _ALLOWLIST.origin_found_in_white_lists(origin, url)


def _set_refresh_cookie(response: Response, value: str, max_age: int) -> None:
    # Also clears it (max_age=0): unlike delete_cookie(), this keeps Secure and
    # HttpOnly on the clearing Set-Cookie.
    response.set_cookie(
        REFRESH_COOKIE,
        value,
        max_age=max_age,
        path=SESSION_PATH,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite='Strict',
    )


def _session_response(tokens: dict) -> Response:
    """``{access}`` in the body, the serializer's ``refresh`` in the cookie."""
    response = Response({'access': tokens['access']})
    lifetime = int(jwt_settings.REFRESH_TOKEN_LIFETIME.total_seconds())
    _set_refresh_cookie(response, tokens['refresh'], lifetime)
    return response


class _CookieSessionView(APIView):
    # No authentication: SessionAuthentication would refuse these POSTs with
    # DRF's own CSRF error whenever a Django admin session cookie is sent along.
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def initial(self, request, *args, **kwargs):
        # The custom header forces a CORS preflight, which only allowlisted
        # origins pass; the Origin check refuses the rest on the server too.
        # Checked before throttling, so forged requests cannot use up the budget.
        origin = request.headers.get('Origin', '')
        if request.headers.get(CSRF_HEADER) != '1' or not _origin_allowed(origin):
            raise CodedError('auth.csrf_failed', status.HTTP_403_FORBIDDEN)
        super().initial(request, *args, **kwargs)


class SessionLoginView(_CookieSessionView):
    """``POST {username, password}`` -> ``{access}`` and the refresh cookie."""

    throttle_scope = 'login'

    def post(self, request):
        serializer = RolesInTokenObtainPairSerializer(
            data=request.data, context={'request': request}
        )
        try:
            serializer.is_valid(raise_exception=True)
        except AuthenticationFailed:
            raise CodedError('auth.invalid_credentials', status.HTTP_401_UNAUTHORIZED) from None
        return _session_response(serializer.validated_data)


class SessionRefreshView(_CookieSessionView):
    """Rotates the refresh cookie (the old token is blacklisted) -> ``{access}``."""

    # Every page load and tab refreshes, so not the login budget (ADR 0017).
    throttle_scope = 'session_refresh'

    def post(self, request):
        refresh = request.COOKIES.get(REFRESH_COOKIE)
        if not refresh:
            raise CodedError('auth.refresh_cookie_missing', status.HTTP_401_UNAUTHORIZED)
        serializer = TokenRefreshSerializer(data={'refresh': refresh})
        try:
            with transaction.atomic():
                # Lock the token's row: two parallel refreshes of one token must
                # not both pass simplejwt's blacklist check and fork the session.
                jti = RefreshToken(refresh)[jwt_settings.JTI_CLAIM]
                OutstandingToken.objects.select_for_update().filter(jti=jti).first()
                serializer.is_valid(raise_exception=True)
        except (TokenError, AuthenticationFailed, get_user_model().DoesNotExist):
            # The cookie is left alone: a parallel refresh from another tab may
            # just have replaced it with a valid one.
            raise CodedError('auth.refresh_cookie_invalid', status.HTTP_401_UNAUTHORIZED) from None
        return _session_response(serializer.validated_data)


class SessionLogoutView(_CookieSessionView):
    """Blacklists the refresh cookie's token, if it is still valid, and clears the cookie."""

    def post(self, request):
        refresh = request.COOKIES.get(REFRESH_COOKIE)
        if refresh:
            try:
                RefreshToken(refresh).blacklist()
            except TokenError:
                pass  # Expired, invalid or already blacklisted: the session is over anyway.
        response = Response({})
        _set_refresh_cookie(response, '', 0)
        return response


_ALLOWLIST = BaseCorsMiddleware(get_response=lambda request: None)


class CorsMiddleware(BaseCorsMiddleware):
    """django-cors-headers, with credentials allowed on the session endpoints only.

    Elsewhere the API is called without cookies, and ``/api/planner/v1/`` answers
    any origin, so ``CORS_ALLOW_CREDENTIALS`` stays off globally. Here the allow
    origin header is only set for allowlisted origins.
    """

    def add_response_headers(self, request, response):
        response = super().add_response_headers(request, response)
        if request.path_info.startswith(SESSION_PATH) and ACCESS_CONTROL_ALLOW_ORIGIN in response:
            response[ACCESS_CONTROL_ALLOW_CREDENTIALS] = 'true'
        return response
