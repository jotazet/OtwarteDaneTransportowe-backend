"""
Niskopoziomowe funkcje pobierania feedów.

Wywoływane wyłącznie przez Celery tasks (data_manager/tasks.py):
  - _fetch_static_entry(entry)          ← fetch_static_entry_task
  - _fetch_realtime_endpoint_rt(ep, now)   ← fetch_realtime_endpoint_task
  - _completed_submission_ids()         ← bootstrap_realtime_tasks, dispatch
  - _completed_realtime_submission_ids()

Nie wywoływać bezpośrednio — logika harmonogramu należy do tasków.
"""

import hashlib
import logging
import os
from urllib.parse import urlparse

import requests
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.files.base import File
from django.utils import timezone

from data_manager.models import (
    FeedFetchError,
    RealtimeEndpointRT,
    StaticFeedEntry,
    completed_realtime_submission_ids,
    completed_submission_ids,
)
from data_manager.net_security import OutboundURLBlocked, safe_download
from data_manager.validators import (
    InvalidFeedContent,
    validate_realtime_feed_stream,
    validate_static_feed_stream,
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
    """Zwraca PKs wszystkich FeedSubmission na etapie 4 (completed)."""
    return completed_submission_ids()


def _completed_realtime_submission_ids() -> list[int]:
    """Zwraca PKs opublikowanych RealtimeSubmission (etap 4)."""
    return completed_realtime_submission_ids()


# ---------------------------------------------------------------------------
# Pobieranie — feed statyczny
# ---------------------------------------------------------------------------


def _delete_stale_cache_file(field_file, old_name: str | None) -> None:
    """Remove the previous cache file when a fetch stored it under a new name.

    Same-name refreshes are replaced atomically by OverwriteStorage and must
    not be deleted here.
    """
    if old_name and old_name != field_file.name and field_file.storage.exists(old_name):
        field_file.storage.delete(old_name)


# Outcome codes returned by the fetch helpers so the Celery task can decide
# whether a retry is warranted. Exceptions are still swallowed here so direct
# callers (management commands) stay resilient.
FETCH_OK = 'ok'
FETCH_TRANSIENT = 'transient'  # timeout / connection / 5xx -> worth retrying
FETCH_PERMANENT = 'permanent'  # blocked URL / 4xx / invalid -> do not retry


def _fetch_static_entry(entry: StaticFeedEntry) -> str:
    """Pobiera plik z URL i zapisuje jako cached_file. Błędy trafiają do FeedFetchError.

    An upstream file identical to the cached copy (a ``304`` to a conditional
    request, or the same SHA-256) is a successful fetch that keeps the copy and
    queues no validation. Without stored validators (see
    ``StaticFeedEntry.CACHE_VALIDATOR_FIELDS``) the file is always saved.

    Zwraca kod wyniku (``FETCH_OK`` / ``FETCH_TRANSIENT`` / ``FETCH_PERMANENT``),
    aby task mógł ponowić tylko błędy przejściowe.
    """
    if not entry.is_proxy_managed:
        logger.info('Skipping static entry=%d — not proxy-managed (hide_original=False)', entry.pk)
        return FETCH_PERMANENT

    from data_manager.tasks import validate_gtfs_feed_task

    # Only a cached copy can stand in for the upstream file.
    compare = bool(entry.cached_file)
    conditional = {}
    if compare and entry.cached_etag:
        conditional['If-None-Match'] = entry.cached_etag
    if compare and entry.cached_last_modified:
        conditional['If-Modified-Since'] = entry.cached_last_modified
    # Passed like the auth headers, so safe_download drops them on a redirect
    # to another origin; the content hash below still catches those feeds.
    headers = {**_build_auth_headers(entry.auth_type, entry.auth_value), **conditional}
    try:
        # safe_download streams the body to a spooled temp file. Buffering a
        # 200 MB feed in memory (twice, as bytes) used to spike every worker.
        with safe_download(
            entry.url,
            headers=headers,
            timeout=60,
            max_bytes=settings.MAX_FEED_FILE_SIZE_BYTES,
            total_timeout=settings.FEED_FETCH_TOTAL_TIMEOUT_SECONDS,
        ) as (response, body):
            # A 304 may update the validators; keep the stored ones otherwise.
            fields = {
                'cached_etag': response.headers.get('ETag') or entry.cached_etag,
                'cached_last_modified': (
                    response.headers.get('Last-Modified') or entry.cached_last_modified
                ),
            }
            unchanged = bool(conditional) and response.status_code == 304
            if not unchanged:
                response.raise_for_status()

                # Reject garbage BEFORE touching the cache: a published feed keeps
                # serving its last good copy when the upstream starts returning
                # HTML error pages or truncated bodies. Only the head is read.
                validate_static_feed_stream(entry.submission.data_type, body)

                sha256 = hashlib.file_digest(body, 'sha256').hexdigest()
                body.seek(0)
                # Upstreams that ignore conditional requests: same bytes, same copy.
                unchanged = compare and sha256 == entry.cached_sha256

            now = timezone.now()
            if not unchanged:
                parsed = urlparse(entry.url)
                filename = os.path.basename(parsed.path) or 'feed.zip'

                # This path bypasses model signals (save=False + queryset.update),
                # so reclaim a differently-named previous cache file explicitly.
                old_cached_name = entry.cached_file.name if entry.cached_file else None
                entry.cached_file.save(filename, File(body), save=False)
                fields.update(
                    cached_file=entry.cached_file.name, cached_at=now, cached_sha256=sha256
                )

        # Only while the entry still proxies the URL just downloaded: a source
        # change during the download drops the cache, and this copy of the
        # old URL must not come back.
        updated = StaticFeedEntry.objects.filter(
            pk=entry.pk, url=entry.url, hide_original=True
        ).update(**fields)
        if not updated:
            current = (
                StaticFeedEntry.objects.filter(pk=entry.pk)
                .values_list('cached_file', flat=True)
                .first()
            )
            if not unchanged and current != entry.cached_file.name:
                entry.cached_file.storage.delete(entry.cached_file.name)
            logger.info('Discarded fetch of static entry=%d: source changed', entry.pk)
            return FETCH_PERMANENT
        entry.mark_fetch_success(now)
        if unchanged:
            logger.info('Static entry=%d unchanged upstream  url=%s', entry.pk, entry.url)
            # A validation the validator could not finish is still retried daily.
            if entry.validation_status in (
                StaticFeedEntry.VALIDATION_PENDING,
                StaticFeedEntry.VALIDATION_ERROR,
            ):
                validate_gtfs_feed_task.delay(entry.pk)
            return FETCH_OK
        _delete_stale_cache_file(entry.cached_file, old_cached_name)
        logger.info('Refreshed static entry=%d  url=%s', entry.pk, entry.url)

        validate_gtfs_feed_task.delay(entry.pk)
        return FETCH_OK

    except (OutboundURLBlocked, InvalidFeedContent) as exc:
        _log_static_error(entry, FeedFetchError.ERROR_INVALID_CONTENT, exc)
        return FETCH_PERMANENT
    except requests.exceptions.Timeout as exc:
        _log_static_error(entry, FeedFetchError.ERROR_TIMEOUT, exc)
        return FETCH_TRANSIENT
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        _log_static_error(entry, FeedFetchError.ERROR_HTTP, exc, http_code=code)
        return FETCH_TRANSIENT if (code and code >= 500) else FETCH_PERMANENT
    except requests.exceptions.ConnectionError as exc:
        _log_static_error(entry, FeedFetchError.ERROR_CONNECTION, exc)
        return FETCH_TRANSIENT
    except Exception as exc:
        _log_static_error(entry, FeedFetchError.ERROR_INVALID_CONTENT, exc)
        return FETCH_PERMANENT


def _log_static_error(entry, error_type, exc, http_code=None) -> None:
    FeedFetchError.objects.create(
        static_entry=entry,
        error_type=error_type,
        http_status_code=http_code,
        message=str(exc),
        url_attempted=entry.url,
    )
    entry.mark_fetch_failure(str(exc))
    logger.warning('Fetch error static entry=%d type=%s: %s', entry.pk, error_type, exc)


# ---------------------------------------------------------------------------
# Pobieranie — endpoint realtime (RealtimeEndpointRT)
# ---------------------------------------------------------------------------


def _fetch_realtime_endpoint_rt(
    endpoint: RealtimeEndpointRT, now, total_timeout: float | None = None
) -> str:
    """Fetch the URL and store it as cached_file. Errors are recorded as FeedFetchError.

    ``total_timeout`` caps the download below REALTIME_FETCH_TOTAL_TIMEOUT_SECONDS
    (realtime verification passes what is left of its budget).
    """
    if not endpoint.is_proxy_managed:
        logger.info(
            'Skipping realtime endpoint_rt=%d — not proxy-managed (hide_original=False)',
            endpoint.pk,
        )
        return FETCH_PERMANENT

    headers = _build_auth_headers(endpoint.auth_type, endpoint.auth_value)
    try:
        with safe_download(
            endpoint.url,
            headers=headers,
            timeout=30,
            max_bytes=settings.MAX_REALTIME_FEED_BYTES,
            total_timeout=total_timeout or settings.REALTIME_FETCH_TOTAL_TIMEOUT_SECONDS,
        ) as (response, body):
            response.raise_for_status()

            # Reject garbage BEFORE touching the cache (see _fetch_static_entry).
            # GTFS-RT is parsed as protobuf here, not merely prefix-checked.
            entity_types = validate_realtime_feed_stream(endpoint.submission.protocol, body)

            # Derive the public cache filename from the URL *path* only — the
            # raw last segment would keep the query string (e.g. '?token=...')
            # and leak upstream credentials into the public download URL.
            filename = os.path.basename(urlparse(endpoint.url).path) or 'feed.pb'
            # Signals are bypassed here (save=False + queryset.update): reclaim
            # a differently-named previous cache file explicitly.
            old_cached_name = endpoint.cached_file.name if endpoint.cached_file else None
            endpoint.cached_file.save(filename, File(body), save=False)
        RealtimeEndpointRT.objects.filter(pk=endpoint.pk).update(
            cached_file=endpoint.cached_file.name,
            cached_at=now,
        )
        _delete_stale_cache_file(endpoint.cached_file, old_cached_name)
        endpoint.record_entity_types(entity_types)
        endpoint.mark_fetch_success(now)
        logger.info('Refreshed realtime endpoint_rt=%d  url=%s', endpoint.pk, endpoint.url)
        return FETCH_OK
    except (OutboundURLBlocked, InvalidFeedContent) as exc:
        _log_endpoint_rt_error(endpoint, FeedFetchError.ERROR_INVALID_CONTENT, exc)
        return FETCH_PERMANENT
    except requests.exceptions.Timeout as exc:
        _log_endpoint_rt_error(endpoint, FeedFetchError.ERROR_TIMEOUT, exc)
        return FETCH_TRANSIENT
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        _log_endpoint_rt_error(endpoint, FeedFetchError.ERROR_HTTP, exc, http_code=code)
        return FETCH_TRANSIENT if (code and code >= 500) else FETCH_PERMANENT
    except requests.exceptions.ConnectionError as exc:
        _log_endpoint_rt_error(endpoint, FeedFetchError.ERROR_CONNECTION, exc)
        return FETCH_TRANSIENT
    except SoftTimeLimitExceeded:
        # The task ran out of time, not the endpoint: no failure (and no pause)
        # is recorded against it.
        raise
    except Exception as exc:
        _log_endpoint_rt_error(endpoint, FeedFetchError.ERROR_INVALID_CONTENT, exc)
        return FETCH_PERMANENT


def _log_endpoint_rt_error(endpoint, error_type, exc, http_code=None) -> None:
    FeedFetchError.objects.create(
        endpoint_rt=endpoint,
        error_type=error_type,
        http_status_code=http_code,
        message=str(exc),
        url_attempted=endpoint.url,
    )
    endpoint.mark_fetch_failure(str(exc))
    logger.warning('Fetch error endpoint_rt=%d type=%s: %s', endpoint.pk, error_type, exc)
