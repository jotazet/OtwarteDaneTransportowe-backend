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
from django.db import models
from django_celery_beat.models import CrontabSchedule, PeriodicTask
import json

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
    # Import deferred to avoid circular dependency
    from data_manager.tasks import validate_gtfs_feed_task

    headers = _build_auth_headers(entry.auth_type, entry.auth_value)
    try:
        response = requests.get(entry.url, headers=headers, timeout=60)
        response.raise_for_status()

        # Determine filename
        import os
        from urllib.parse import urlparse
        parsed = urlparse(entry.url)
        filename = os.path.basename(parsed.path) or 'feed.zip'

        # Save file to storage
        # Note: entry.cached_file.save() saves to storage backend.
        # But we want to update the DB field 'cached_file' as well.
        # The 'save=True' arg (default) triggers model.save(), which triggers signals.
        # However, original code used save=False + update(), probably to avoid signals or other side effects.
        # But signals are REQUIRED for validation if we rely on signals.py.
        # If we use update(), we MUST manually trigger the task.

        entry.cached_file.save(filename, ContentFile(response.content), save=False)

        # Update timestamp and file path in DB
        StaticFeedEntry.objects.filter(pk=entry.pk).update(
            cached_file=entry.cached_file.name,
            cached_at=timezone.now(),
        )
        logger.info('Refreshed static entry=%d  url=%s', entry.pk, entry.url)

        # Trigger validation manually since update() bypassed signals
        validate_gtfs_feed_task.delay(entry.pk)

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


# ---------------------------------------------------------------------------
# Okresowe zadaania pobierania
# ---------------------------------------------------------------------------

def setup_periodic_tasks(sender, **kwargs) -> None:
    """Tworzy zadania okresowe dla aktywnych statycznych źródeł danych."""
    from data_manager.models import StaticFeedEntry

    # Usuwa istniejące zadania, które mogły zostać utworzone przez ten mechanizm
    PeriodicTask.objects.filter(name__startswith='fetch-static-entry-').delete()

    # Aktywne źródła danych, które mają ustawiony przynajmniej jeden czas pobierania
    entries = StaticFeedEntry.objects.filter(
        models.Q(download_time_1__isnull=False) | models.Q(download_time_2__isnull=False),
        submission__is_rejected=False,
        hide_original=True
    ).select_related('submission')

    for entry in entries:
        times = [entry.download_time_1, entry.download_time_2]
        for i, time in enumerate(times, 1):
            if time:
                # Tworzenie harmonogramu Crontab
                schedule, _ = CrontabSchedule.objects.get_or_create(
                    minute=time.minute,
                    hour=time.hour,
                    day_of_week='*',
                    day_of_month='*',
                    month_of_year='*',
                )

                # Tworzenie zadania okresowego
                PeriodicTask.objects.create(
                    crontab=schedule,
                    name=f'fetch-static-entry-{entry.id}-time-{i}',
                    task='data_manager.tasks.fetch_static_entry_task',
                    args=json.dumps([entry.id]),
                )
                logger.info(f"Scheduled static fetch for entry {entry.id} at {time.hour}:{time.minute}")
