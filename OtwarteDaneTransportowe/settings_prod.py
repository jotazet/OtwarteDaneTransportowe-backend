from django.core.exceptions import ImproperlyConfigured

from .settings_base import *  # noqa
from .settings_base import (  # noqa: F401
    _env_bool,
    _env_list,
    INSECURE_SECRET_KEY_PLACEHOLDER,
    validate_production_secret_key,
)

REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

DEBUG = False

# Fail fast: never run production with a weak, example or placeholder key.
# (Catches e.g. the 'change-me' value from .env.example, which docker-compose
# loads as an env_file fallback whenever .env is missing.)
validate_production_secret_key(SECRET_KEY)  # noqa: F405

# Same idea for the database password: docker-compose falls back to
# .env.example, so a missing .env must not silently deploy example credentials.
if DATABASES['default']['PASSWORD'] in {'change-me', 'postgres', ''}:  # noqa: F405
    raise ImproperlyConfigured(
        'POSTGRES_PASSWORD must be set to a strong, unique value in production. '
        'Refusing to start with an example/default database password.'
    )

# Security hardening (minimal baseline)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

SECURE_HSTS_SECONDS = int(os.getenv('DJANGO_SECURE_HSTS_SECONDS', '31536000'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# TLS is terminated by the external reverse-proxy (see DEPLOYMENT.md). Enabling
# redirect here breaks in-container HTTP healthchecks and is redundant when the
# proxy already upgrades HTTP→HTTPS. Set DJANGO_SECURE_SSL_REDIRECT=True only if
# the app is exposed directly to clients without a TLS-terminating proxy.
SECURE_SSL_REDIRECT = _env_bool('DJANGO_SECURE_SSL_REDIRECT', False)
SESSION_COOKIE_HTTPONLY = True

# Healthchecks and the local reverse-proxy reach the app on loopback.
_INTERNAL_ALLOWED_HOSTS = ('127.0.0.1', 'localhost')
ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, *_INTERNAL_ALLOWED_HOSTS]))

# Required for POST/CSRF behind a TLS-terminating reverse proxy (nginx/Caddy).
# Provide full scheme://host origins, e.g. https://api.example.org
CSRF_TRUSTED_ORIGINS = _env_list('CSRF_TRUSTED_ORIGINS', default=[])

# This file already assumes a reverse proxy (SECURE_PROXY_SSL_HEADER above), so
# an empty TRUSTED_PROXY_CIDRS is a misconfiguration, not a default. Without it
# get_client_ip() ignores X-Forwarded-For and every visitor is seen as the
# proxy's own address, which silently:
#   * collapses all visitors into ONE blog-reaction identity (the "10 per day
#     per IP" limit becomes 10 per day for the entire internet, and the
#     unique-per-(post, IP) constraint keeps one row for everybody), and
#   * puts all anonymous traffic in a single AnonRateThrottle bucket.
# blog.checks.check_trusted_proxy_cidrs already reports this as a Django system
# check (blog.W001), but a Warning does not stop a deploy and is easy to lose in
# container logs. This makes it fatal for the settings module that explicitly
# assumes a proxy; the check still covers other DEBUG=False configurations.
#
# Set it to the proxy's address(es), e.g. TRUSTED_PROXY_CIDRS=10.0.0.0/8, or
# opt out explicitly with ALLOW_UNTRUSTED_PROXY_IPS=True when the app really is
# exposed directly to clients.
if not TRUSTED_PROXY_CIDRS and not _env_bool('ALLOW_UNTRUSTED_PROXY_IPS', False):  # noqa: F405
    raise ImproperlyConfigured(
        'TRUSTED_PROXY_CIDRS is empty while the app is configured to run behind '
        'a reverse proxy. Client IPs would all resolve to the proxy, breaking '
        'per-IP reaction limits and anonymous throttling. Set TRUSTED_PROXY_CIDRS '
        'to your proxy network(s), or set ALLOW_UNTRUSTED_PROXY_IPS=True if the '
        'app is served directly to clients.'
    )

# Modest extra hardening; neither affects the API contract.
SECURE_REFERRER_POLICY = os.getenv('DJANGO_SECURE_REFERRER_POLICY', 'same-origin')  # noqa: F405
SECURE_CROSS_ORIGIN_OPENER_POLICY = os.getenv(  # noqa: F405
    'DJANGO_SECURE_COOP', 'same-origin'
)

# Container-friendly logging: structured-ish output to stdout/stderr.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}

