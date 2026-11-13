"""
Low-level feed fetching functions.

Called from the Celery tasks (data_manager/tasks.py) and the refresh_feeds
management command:
  - _fetch_static_entry(entry)             <- fetch_static_entry_task, refresh_feeds
  - _fetch_realtime_endpoint_rt(ep, now)   <- fetch_realtime_endpoint_task,
                                              validate_realtime_submission_task
  - _completed_submission_ids()            <- refresh_static_feeds_task, refresh_feeds
  - _completed_realtime_submission_ids()   <- bootstrap_realtime_tasks,
                                              fetch_realtime_endpoint_task

They fetch once and record the result; when to fetch (schedules, retries,
back-off) is decided by the tasks.
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

from data_manager.fields import CredentialUndecryptable, UndecryptableValue
from data_manager.models import (
    FETCH_RETRY_DELAYS,
    REALTIME_TRANSIENT_RETRY_DELAYS,
    FeedFetchError,
    RealtimeEndpointRT,
    StaticFeedEntry,
    completed_realtime_submission_ids,
    completed_submission_ids,
)
from data_manager.net_security import OutboundURLBlocked, redact_url, redact_urls, safe_download
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
    """HTTP headers for the source's auth type.

    Raises ``CredentialUndecryptable`` when the stored credential cannot be decrypted.
    """
    if auth_type and isinstance(auth_value, UndecryptableValue):
        raise CredentialUndecryptable
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
FETCH_TRANSIENT = 'transient'  # timeout / connection / 5xx / 429 -> worth retrying
FETCH_PERMANENT = 'permanent'  # blocked URL / other 4xx / invalid -> do not retry


def _http_error_outcome(code: int | None) -> str:
    """5xx and 429 Too Many Requests are the upstream's passing trouble."""
    return FETCH_TRANSIENT if code and (code >= 500 or code == 429) else FETCH_PERMANENT


def _fetch_static_entry(entry: StaticFeedEntry) -> str:
    """Download the file from its URL and save it as cached_file. Errors go to FeedFetchError.

    An upstream file identical to the cached copy (a ``304`` to a conditional
    request, or the same SHA-256) is a successful fetch that keeps the copy and
    queues no validation. Without stored validators (see
    ``StaticFeedEntry.CACHE_VALIDATOR_FIELDS``) the file is always saved.

    Returns a result code (``FETCH_OK`` / ``FETCH_TRANSIENT`` / ``FETCH_PERMANENT``)
    so that the task retries only transient errors.
    """
    if not entry.is_proxy_managed:
        logger.info('Skipping static entry=%d — not proxy-managed (hide_original=False)', entry.pk)
        return FETCH_PERMANENT

    from data_manager.tasks import enqueue, validate_gtfs_feed_task

    # Only a cached copy can stand in for the upstream file.
    compare = bool(entry.cached_file)
    conditional = {}
    if compare and entry.cached_etag:
        conditional['If-None-Match'] = entry.cached_etag
    if compare and entry.cached_last_modified:
        conditional['If-Modified-Since'] = entry.cached_last_modified
    try:
        # Passed like the auth headers, so safe_download drops them on a redirect
        # to another origin; the content hash below still catches those feeds.
        headers = {**_build_auth_headers(entry.auth_type, entry.auth_value), **conditional}
        # safe_download streams the body to a spooled temp file. Buffering a
        # 200 MB feed in memory (twice, as bytes) used to spike every worker.
        with safe_download(
            entry.url,
            headers=headers,
            timeout=60,
            max_bytes=settings.MAX_FEED_FILE_SIZE_BYTES,
            total_timeout=settings.FEED_FETCH_TOTAL_TIMEOUT_SECONDS,
        ) as (response, body):
            etag = response.headers.get('ETag', '')
            last_modified = response.headers.get('Last-Modified', '')
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
            if unchanged:
                # Validators the response leaves out still describe the kept copy.
                fields = {
                    'cached_etag': etag or entry.cached_etag,
                    'cached_last_modified': last_modified or entry.cached_last_modified,
                }
            else:
                parsed = urlparse(entry.url)
                filename = os.path.basename(parsed.path) or 'feed.zip'

                # This path bypasses model signals (save=False + queryset.update),
                # so reclaim a differently-named previous cache file explicitly.
                old_cached_name = entry.cached_file.name if entry.cached_file else None
                entry.cached_file.save(filename, File(body), save=False)
                # A new copy keeps only its own validators.
                fields = {
                    'cached_file': entry.cached_file.name,
                    'cached_at': now,
                    'cached_sha256': sha256,
                    'cached_etag': etag,
                    'cached_last_modified': last_modified,
                }

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
            logger.info(
                'Static entry=%d unchanged upstream  url=%s', entry.pk, redact_url(entry.url)
            )
            # A validation the validator could not finish is still retried daily.
            validate = entry.validation_status in (
                StaticFeedEntry.VALIDATION_PENDING,
                StaticFeedEntry.VALIDATION_ERROR,
            )
        else:
            _delete_stale_cache_file(entry.cached_file, old_cached_name)
            logger.info('Refreshed static entry=%d  url=%s', entry.pk, redact_url(entry.url))
            validate = True

    except CredentialUndecryptable as exc:
        _log_static_error(entry, FeedFetchError.ERROR_AUTH, exc)
        return FETCH_PERMANENT
    except (OutboundURLBlocked, InvalidFeedContent) as exc:
        _log_static_error(entry, FeedFetchError.ERROR_INVALID_CONTENT, exc)
        return FETCH_PERMANENT
    except requests.exceptions.Timeout as exc:
        _log_static_error(entry, FeedFetchError.ERROR_TIMEOUT, exc)
        return FETCH_TRANSIENT
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        _log_static_error(entry, FeedFetchError.ERROR_HTTP, exc, http_code=code)
        return _http_error_outcome(code)
    except requests.exceptions.ConnectionError as exc:
        _log_static_error(entry, FeedFetchError.ERROR_CONNECTION, exc)
        return FETCH_TRANSIENT
    except Exception as exc:
        _log_static_error(entry, FeedFetchError.ERROR_INVALID_CONTENT, exc)
        return FETCH_PERMANENT

    # Outside the try: a broker error is not a failed fetch (enqueue() logs it;
    # see retry_gtfs_validation). Callers run in autocommit, so the rows above
    # are committed already.
    if validate:
        enqueue(validate_gtfs_feed_task, entry.pk)
    return FETCH_OK


def _log_static_error(entry, error_type, exc, http_code=None) -> None:
    # Requests put the URL, query string included, in their exception texts.
    message = redact_urls(str(exc))
    FeedFetchError.objects.create(
        static_entry=entry,
        error_type=error_type,
        http_status_code=http_code,
        message=message,
        url_attempted=redact_url(entry.url),
    )
    entry.mark_fetch_failure(message)
    logger.warning('Fetch error static entry=%d type=%s: %s', entry.pk, error_type, message)


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

    try:
        with safe_download(
            endpoint.url,
            headers=_build_auth_headers(endpoint.auth_type, endpoint.auth_value),
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
        # DEBUG: this runs on every poll, every few seconds. No URL: it may carry a token.
        logger.debug('Refreshed realtime endpoint_rt=%d', endpoint.pk)
        return FETCH_OK
    except CredentialUndecryptable as exc:
        return _log_endpoint_rt_error(endpoint, FeedFetchError.ERROR_AUTH, exc, FETCH_PERMANENT)
    except (OutboundURLBlocked, InvalidFeedContent) as exc:
        return _log_endpoint_rt_error(
            endpoint, FeedFetchError.ERROR_INVALID_CONTENT, exc, FETCH_PERMANENT
        )
    except requests.exceptions.Timeout as exc:
        return _log_endpoint_rt_error(endpoint, FeedFetchError.ERROR_TIMEOUT, exc, FETCH_TRANSIENT)
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        return _log_endpoint_rt_error(
            endpoint, FeedFetchError.ERROR_HTTP, exc, _http_error_outcome(code), http_code=code
        )
    except requests.exceptions.ConnectionError as exc:
        return _log_endpoint_rt_error(
            endpoint, FeedFetchError.ERROR_CONNECTION, exc, FETCH_TRANSIENT
        )
    except SoftTimeLimitExceeded:
        # The task ran out of time, not the endpoint: no failure (and no pause)
        # is recorded against it.
        raise
    except Exception as exc:
        return _log_endpoint_rt_error(
            endpoint, FeedFetchError.ERROR_INVALID_CONTENT, exc, FETCH_PERMANENT
        )


def _log_endpoint_rt_error(endpoint, error_type, exc, outcome, http_code=None) -> str:
    """Record the error and back off on the ladder of its class; returns ``outcome``."""
    # Requests put the URL, query string included, in their exception texts.
    message = redact_urls(str(exc))
    FeedFetchError.objects.create(
        endpoint_rt=endpoint,
        error_type=error_type,
        http_status_code=http_code,
        message=message,
        url_attempted=redact_url(endpoint.url),
    )
    transient = outcome == FETCH_TRANSIENT
    delays = REALTIME_TRANSIENT_RETRY_DELAYS if transient else FETCH_RETRY_DELAYS
    endpoint.mark_fetch_failure(message, delays=delays)
    logger.warning('Fetch error endpoint_rt=%d type=%s: %s', endpoint.pk, error_type, message)
    return outcome
