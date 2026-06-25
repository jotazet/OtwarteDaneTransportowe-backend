import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

INSECURE_SECRET_KEY_PLACEHOLDER = 'django-insecure-dev-placeholder'

# Path to the project root on the HOST machine.
HOST_PROJECT_PATH = os.environ.get('HOST_PROJECT_PATH', str(BASE_DIR))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return default or []
    items = [x.strip() for x in raw.split(',')]
    return [x for x in items if x]


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', INSECURE_SECRET_KEY_PLACEHOLDER)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool('DJANGO_DEBUG', False)

ALLOWED_HOSTS = _env_list('DJANGO_ALLOWED_HOSTS', default=['127.0.0.1', 'localhost'])


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',

    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'django_filters',
    'drf_spectacular',
    'drf_spectacular_sidecar',
    'django_celery_beat',
    'corsheaders',

    # Local apps
    'blog.apps.BlogConfig',
    'cases.apps.CasesConfig',
    'data_manager.apps.DataManagerConfig',
    'accounts.apps.AccountsConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'OtwarteDaneTransportowe.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [str(BASE_DIR / 'Portal')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'OtwarteDaneTransportowe.wsgi.application'

SPECTACULAR_SETTINGS = {
    'SWAGGER_UI_DIST': 'SIDECAR',
    'SWAGGER_UI_FAVICON_HREF': 'SIDECAR',
    'REDOC_DIST': 'SIDECAR',
}


USE_SQLITE = os.getenv('USE_SQLITE', '').strip().lower() in {'1', 'true', 'yes', 'on'}
if USE_SQLITE:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(BASE_DIR / 'db.sqlite3'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('POSTGRES_DB', 'otwarte_dane_transportowe'),
            'USER': os.getenv('POSTGRES_USER', 'postgres'),
            'PASSWORD': os.getenv('POSTGRES_PASSWORD', 'postgres'),
            'HOST': os.getenv('POSTGRES_HOST', '127.0.0.1'),
            'PORT': os.getenv('POSTGRES_PORT', '5420'),
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = str(BASE_DIR / 'staticfiles')

MEDIA_URL = '/internal-media/'
# Default under project root; override e.g. for unusual layouts via DJANGO_MEDIA_ROOT.
MEDIA_ROOT = os.environ.get('DJANGO_MEDIA_ROOT', str(BASE_DIR / 'uploaded_data'))

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_FILTER_BACKENDS': ['django_filters.rest_framework.DjangoFilterBackend'],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': os.getenv('THROTTLE_ANON', '60/min'),
        'user': os.getenv('THROTTLE_USER', '240/min'),
        # Scoped buckets (applied via throttle_scope on specific views).
        'login': os.getenv('THROTTLE_LOGIN', '10/min'),
        'feed_download': os.getenv('THROTTLE_FEED_DOWNLOAD', '120/min'),
    },
}

# Cache backend. Must be a shared backend (Redis) in production so that the
# realtime self-scheduling lock and DRF throttling work across processes/workers.
# Falls back to per-process local memory only when REDIS_CACHE_URL is unset (dev/tests).
REDIS_CACHE_URL = os.getenv('REDIS_CACHE_URL')
if REDIS_CACHE_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_CACHE_URL,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }

# Upload size limits (defence against zip bombs / disk-fill / OOM).
# Bytes held in memory before streaming to a temp file.
FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv('FILE_UPLOAD_MAX_MEMORY_SIZE', str(5 * 1024 * 1024)))
# Hard cap on non-file request body size.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv('DATA_UPLOAD_MAX_MEMORY_SIZE', str(10 * 1024 * 1024)))
# Hard cap (bytes) for any single feed file (uploaded by a user OR fetched by the
# server from a remote URL). Enforced by validators and the fetch helpers.
MAX_FEED_FILE_SIZE_BYTES = int(os.getenv('MAX_FEED_FILE_SIZE_BYTES', str(200 * 1024 * 1024)))
# Hard cap (bytes) for blog post images.
MAX_IMAGE_FILE_SIZE_BYTES = int(os.getenv('MAX_IMAGE_FILE_SIZE_BYTES', str(10 * 1024 * 1024)))

from datetime import timedelta  # noqa: E402

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=int(os.getenv('JWT_ACCESS_MINUTES', '15'))),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=int(os.getenv('JWT_REFRESH_DAYS', '1'))),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
}

# Celery
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/1')
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    'refresh-static-feeds': {
        'task': 'data_manager.refresh_static_feeds',
        'schedule': crontab(),
        'options': {'queue': 'feeds'},
    },
    'bootstrap-realtime-tasks': {
        'task': 'data_manager.bootstrap_realtime_tasks',
        'schedule': crontab(minute='*/5'),
        'options': {'queue': 'feeds'},
    },
}

CELERY_TASK_QUEUES = {
    'default': {},
    'feeds': {},
}
CELERY_TASK_DEFAULT_QUEUE = 'default'
CELERY_TASK_ROUTES = {
    'data_manager.validate_gtfs_feed': {'queue': 'feeds'},
    'data_manager.fetch_static_entry': {'queue': 'feeds'},
}
CELERY_TASK_SOFT_TIME_LIMIT = 90
CELERY_TASK_TIME_LIMIT = 120


# Fernet key used by data_manager.fields.EncryptedCharField to encrypt stored
# feed auth credentials at rest. When unset, values fall back to plaintext
# (development/testing only). Production MUST set this.
FEED_AUTH_ENCRYPTION_KEY = os.getenv('FEED_AUTH_ENCRYPTION_KEY')

# Proxy settings (used by blog reactions IP limiting)
TRUSTED_PROXY_CIDRS = _env_list('TRUSTED_PROXY_CIDRS', default=[])

CORS_ALLOWED_ORIGINS = _env_list('CORS_ALLOWED_ORIGINS', default=[])
CORS_URLS_REGEX = os.getenv('CORS_URLS_REGEX', r'^/(api|feed)/.*$')

# Permissive localhost origins are only enabled in development. In production
# rely solely on the explicit CORS_ALLOWED_ORIGINS allowlist.
if DEBUG:
    CORS_ALLOWED_ORIGIN_REGEXES = [
        r"^http://localhost:\d+$",
        r"^http://127\.0\.0\.1:\d+$",
    ]
else:
    CORS_ALLOWED_ORIGIN_REGEXES = _env_list('CORS_ALLOWED_ORIGIN_REGEXES', default=[])
