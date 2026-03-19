"""
Niskopoziomowe funkcje pobierania feedów.

Wywoływane wyłącznie przez Celery tasks (data_manager/tasks.py):
  - _fetch_static_entry(entry)          ← fetch_static_entry_task
  - _fetch_realtime_endpoint(ep, now)   ← fetch_realtime_endpoint_task
  - _completed_submission_ids()         ← bootstrap_realtime_tasks, dispatch

Nie wywoływać bezpośrednio — logika harmonogramu należy do tasków.
"""
import logging

import requests
from django.core.files.base import ContentFile
from django.utils import timezone

from data_manager.models import (
    FeedFetchError,
    FeedSubmissionHistory,
    RealtimeEndpoint,
    StaticFeedEntry,
    completed_submission_ids,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: autoryzacja
# ---------------------------------------------------------------------------

def _build_auth_headers(auth_type: str, auth_value: str | None) -> dict:
    """Buduje nagłówki HTTP na podstawie wybranego typu autentykacji."""
    if auth_type == 'api_key' and auth_value:
        return {'X-API-Key': auth_value}
    if auth_type == 'bearer_token' and auth_value:
        return {'Authorization': f'Bearer {auth_value}'}
    if auth_type == 'basic_auth' and auth_value:
        import base64
        encoded = base64.b64encode(auth_value.encode()).decode()
        return {'Authorization': f'Basic {encoded}'}
    return {}


# ---------------------------------------------------------------------------
# Helper: zatwierdzone submission IDs
# ---------------------------------------------------------------------------

def _completed_submission_ids() -> list[int]:
    """Zwraca PKs wszystkich submissionów na etapie 4 (completed)."""
    return completed_submission_ids()


# ---------------------------------------------------------------------------
# Pobieranie — feed statyczny
# ---------------------------------------------------------------------------

def _fetch_static_entry(entry: StaticFeedEntry) -> None:
    """Pobiera plik z URL i zapisuje jako cached_file. Błędy trafiają do FeedFetchError."""
    headers = _build_auth_headers(entry.auth_type, entry.auth_value)
    try:
        response = requests.get(entry.url, headers=headers, timeout=60)
        response.raise_for_status()
        filename = entry.url.rstrip('/').split('/')[-1] or 'feed.zip'
        entry.cached_file.save(filename, ContentFile(response.content), save=False)
        StaticFeedEntry.objects.filter(pk=entry.pk).update(
            cached_file=entry.cached_file.name,
            cached_at=timezone.now(),
        )
        logger.info('Refreshed static entry=%d  url=%s', entry.pk, entry.url)
    except requests.exceptions.Timeout as exc:
        _log_static_error(entry, FeedFetchError.ERROR_TIMEOUT, exc)
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        _log_static_error(entry, FeedFetchError.ERROR_HTTP, exc, http_code=code)
    except requests.exceptions.ConnectionError as exc:
        _log_static_error(entry, FeedFetchError.ERROR_CONNECTION, exc)
    except Exception as exc:
        _log_static_error(entry, FeedFetchError.ERROR_INVALID_CONTENT, exc)


def _log_static_error(entry, error_type, exc, http_code=None) -> None:
    FeedFetchError.objects.create(
        static_entry=entry,
        error_type=error_type,
        http_status_code=http_code,
        message=str(exc),
        url_attempted=entry.url,
    )
    logger.warning('Fetch error static entry=%d type=%s: %s', entry.pk, error_type, exc)


# ---------------------------------------------------------------------------
# Pobieranie — endpoint realtime
# ---------------------------------------------------------------------------

def _fetch_realtime_endpoint(endpoint: RealtimeEndpoint, now) -> None:
    """Pobiera plik z URL i zapisuje jako cached_file. Błędy trafiają do FeedFetchError."""
    headers = _build_auth_headers(endpoint.auth_type, endpoint.auth_value)
    try:
        response = requests.get(endpoint.url, headers=headers, timeout=30)
        response.raise_for_status()
        filename = endpoint.url.rstrip('/').split('/')[-1] or 'feed.pb'
        endpoint.cached_file.save(filename, ContentFile(response.content), save=False)
        RealtimeEndpoint.objects.filter(pk=endpoint.pk).update(
            cached_file=endpoint.cached_file.name,
            cached_at=now,
        )
        logger.info('Refreshed realtime endpoint=%d  url=%s', endpoint.pk, endpoint.url)
    except requests.exceptions.Timeout as exc:
        _log_endpoint_error(endpoint, FeedFetchError.ERROR_TIMEOUT, exc)
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        _log_endpoint_error(endpoint, FeedFetchError.ERROR_HTTP, exc, http_code=code)
    except requests.exceptions.ConnectionError as exc:
        _log_endpoint_error(endpoint, FeedFetchError.ERROR_CONNECTION, exc)
    except Exception as exc:
        _log_endpoint_error(endpoint, FeedFetchError.ERROR_INVALID_CONTENT, exc)


def _log_endpoint_error(endpoint, error_type, exc, http_code=None) -> None:
    FeedFetchError.objects.create(
        endpoint=endpoint,
        error_type=error_type,
        http_status_code=http_code,
        message=str(exc),
        url_attempted=endpoint.url,
    )
    logger.warning('Fetch error endpoint=%d type=%s: %s', endpoint.pk, error_type, exc)
