"""Access control of the public planner API: keys, bans, limits and the request log.

Every public planner view extends :class:`PlannerPublicView`. See ADR 0010 and
docs/api/planner.md (access, limits and errors).
"""

import hashlib
import ipaddress
import logging
import time

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView, exception_handler

from OtwarteDaneTransportowe.request_ip import get_client_ip
from planner import services
from planner.models import ApiKey, IpBan, PlannerRequestLog, PlannerSettings

logger = logging.getLogger(__name__)

API_KEY_HEADER = 'X-API-Key'
SETTINGS_CACHE_KEY = 'planner:settings'
BANS_CACHE_KEY = 'planner:bans'
ACCESS_CACHE_SECONDS = 30
LAST_USED_EVERY_SECONDS = 300
# A client hammering the API while limited or banned is logged once a minute.
REJECTION_LOG_EVERY_SECONDS = 60
MAINTENANCE_RETRY_AFTER_SECONDS = 600


class PlannerError(exceptions.APIException):
    """An error of the public planner API, rendered as ``{code, details}`` (ADR 0006)."""

    def __init__(self, code: str, http_status: int, details=None, retry_after=None):
        super().__init__(code)
        self.code = code
        self.status_code = http_status
        self.details = details or {}
        self.wait = retry_after


def planner_exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        response = Response({'code': exc.code, 'details': exc.details}, status=exc.status_code)
        if exc.wait:
            response['Retry-After'] = str(int(exc.wait))
        return response
    if isinstance(exc, exceptions.ValidationError):
        return Response(
            {'code': 'planner.invalid_request', 'details': {'fields': exc.get_codes()}},
            status=400,
        )
    response = exception_handler(exc, context)
    if response is not None and isinstance(exc, exceptions.APIException):
        code = exc.get_codes() if isinstance(exc.get_codes(), str) else exc.default_code
        response.data = {'code': f'planner.{code}', 'details': {}}
    return response


# ---------------------------------------------------------------------------
# Cached settings and bans
# ---------------------------------------------------------------------------


def planner_settings() -> PlannerSettings:
    cached = cache.get(SETTINGS_CACHE_KEY)
    if cached is None:
        cached = PlannerSettings.load()
        cache.set(SETTINGS_CACHE_KEY, cached, ACCESS_CACHE_SECONDS)
    return cached


def active_bans() -> list[tuple[str, float | None]]:
    """(network, expiry timestamp or None) of every ban that has not expired."""
    cached = cache.get(BANS_CACHE_KEY)
    if cached is None:
        now = timezone.now()
        cached = [
            (ban.network, ban.expires_at.timestamp() if ban.expires_at else None)
            for ban in IpBan.objects.all()
            if ban.expires_at is None or ban.expires_at > now
        ]
        cache.set(BANS_CACHE_KEY, cached, ACCESS_CACHE_SECONDS)
    return cached


def forget_cached_access() -> None:
    """Call after admins change settings or bans, so the change applies at once."""
    cache.delete_many([SETTINGS_CACHE_KEY, BANS_CACHE_KEY])


def client_identity(request) -> tuple[str | None, str]:
    """(client IP for the log, identity for limits): IPv6 clients count per /64."""
    ip = get_client_ip(request)
    try:
        address = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return None, str(ip or 'unknown')
    if address.version == 6:
        return ip, str(ipaddress.ip_network(f'{address}/64', strict=False))
    return ip, ip


def is_banned(ip: str | None) -> bool:
    if ip is None:
        return False
    address = ipaddress.ip_address(ip)
    now = time.time()
    return any(
        address in ipaddress.ip_network(network) and (expiry is None or expiry > now)
        for network, expiry in active_bans()
    )


# ---------------------------------------------------------------------------
# DRF building blocks
# ---------------------------------------------------------------------------


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiKeyAuthentication(BaseAuthentication):
    """Optional ``X-API-Key``; without it the client is anonymous (per-IP limits)."""

    def authenticate(self, request):
        raw = request.headers.get(API_KEY_HEADER)
        if not raw:
            return None
        key = ApiKey.objects.filter(key_hash=hash_key(raw), is_active=True).first()
        if key is None:
            raise PlannerError('planner.invalid_api_key', 401)
        if cache.add(f'planner:key-used:{key.id}', 1, LAST_USED_EVERY_SECONDS):
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        return AnonymousUser(), key

    def authenticate_header(self, request):
        return API_KEY_HEADER


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    """Documents the optional API key in the planner's OpenAPI schema."""

    target_class = ApiKeyAuthentication
    name = 'ApiKey'

    def get_security_definition(self, auto_schema):
        return {'type': 'apiKey', 'in': 'header', 'name': API_KEY_HEADER}


class PlannerPublicAccess(BasePermission):
    """Kill switch, bans and the build window, in that order."""

    def has_permission(self, request, view):
        if not planner_settings().public_api_enabled and not view.available_when_disabled:
            raise PlannerError('planner.disabled', 503)
        if is_banned(client_identity(request)[0]):
            raise PlannerError('planner.client_banned', 403)
        if view.uses_otp and services.maintenance_active():
            raise PlannerError(
                'planner.unavailable',
                503,
                details={'reason': 'maintenance'},
                retry_after=MAINTENANCE_RETRY_AFTER_SECONDS,
            )
        return True


class PlannerRateThrottle(BaseThrottle):
    """Requests per minute, per client and for everyone together, per endpoint group."""

    def allow_request(self, request, view):
        group = view.limit_group
        limits = planner_settings()
        key = request.auth if isinstance(request.auth, ApiKey) else None
        if key is not None:
            ident = f'key:{key.id}'
            limit = getattr(key, f'{group}_per_minute') or limits.limit('key', group)
        else:
            ident = f'ip:{client_identity(request)[1]}'
            limit = limits.limit('anonymous', group)
        now = time.time()
        # Minute-stamped keys: a counter that loses its expiry only affects a past minute.
        minute = int(now // 60)
        retry_after = 60 - int(now % 60)
        if _count(f'planner:rl:{group}:{ident}:{minute}') > limit:
            raise PlannerError('planner.rate_limited', 429, {'scope': 'client'}, retry_after)
        if _count(f'planner:rl:{group}:all:{minute}') > limits.limit('global', group):
            raise PlannerError('planner.rate_limited', 429, {'scope': 'global'}, retry_after)
        return True


def _count(key: str) -> int:
    cache.add(key, 0, 120)
    try:
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, 120)
        return 1


class PlannerSchema(AutoSchema):
    def get_auth(self):
        # The key only raises the limits: every endpoint also works without it.
        auth = super().get_auth()
        return auth if {} in auth else [*auth, {}]


class PlannerPublicView(APIView):
    """Base of every public planner endpoint."""

    schema = PlannerSchema()

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny, PlannerPublicAccess]
    throttle_classes = [PlannerRateThrottle]
    # 'plan', 'realtime' or 'lookup': which limits apply.
    limit_group = 'lookup'
    # Answers from OTP, so unavailable while a build has it stopped.
    uses_otp = False
    # Still answers when the kill switch is off (the status endpoint).
    available_when_disabled = False

    def get_exception_handler(self):
        return planner_exception_handler

    def dispatch(self, request, *args, **kwargs):
        started = time.monotonic()
        response = super().dispatch(request, *args, **kwargs)
        try:
            _log_request(self.request, response, started)
        except Exception:  # noqa: BLE001 - logging must never break the response
            logger.exception('Could not log a planner request')
        return response


def _log_request(request, response, started: float) -> None:
    ip, ident = client_identity(request)
    if response.status_code in (403, 429) and not cache.add(
        f'planner:rejection-logged:{ident}:{response.status_code}', 1, REJECTION_LOG_EVERY_SECONDS
    ):
        return
    match = getattr(request, 'resolver_match', None)
    PlannerRequestLog.objects.create(
        endpoint=(match.url_name if match else '') or '',
        status_code=response.status_code,
        latency_ms=int((time.monotonic() - started) * 1000),
        client_ip=ip,
        api_key=request.auth if isinstance(request.auth, ApiKey) else None,
        user_agent=request.headers.get('User-Agent', '')[:256],
    )
