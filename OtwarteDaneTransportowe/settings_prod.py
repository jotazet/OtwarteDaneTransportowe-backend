from django.core.exceptions import ImproperlyConfigured

from .settings_base import *  # noqa
from .settings_base import _env_bool, _env_list, INSECURE_SECRET_KEY_PLACEHOLDER  # noqa: F401

DEBUG = False

# Fail fast: never run production with the insecure development placeholder key.
if SECRET_KEY == INSECURE_SECRET_KEY_PLACEHOLDER:  # noqa: F405
    raise ImproperlyConfigured(
        'DJANGO_SECRET_KEY must be set to a strong, unique value in production. '
        'Refusing to start with the insecure development placeholder.'
    )

# Security hardening (minimal baseline)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'

SECURE_HSTS_SECONDS = int(os.getenv('DJANGO_SECURE_HSTS_SECONDS', '31536000'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_SSL_REDIRECT = _env_bool('DJANGO_SECURE_SSL_REDIRECT', True)
SESSION_COOKIE_HTTPONLY = True

# Required for POST/CSRF behind a TLS-terminating reverse proxy (nginx/Caddy).
# Provide full scheme://host origins, e.g. https://api.example.org
CSRF_TRUSTED_ORIGINS = _env_list('CSRF_TRUSTED_ORIGINS', default=[])

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

