"""
Celery tasks: fetching and refreshing the feed cache.

Task layout:
─────────────────────────────────────────────────────────────
  STATIC (download_time_1/2):
  ┌─ Beat every minute ──▶ refresh_static_feeds_task()
  │                          checks download_time_1/2 == now
  │                          └─ .delay() ──▶ fetch_static_entry_task(id)
  └─────────────────────────────────────────────────────────

  REALTIME (interval per endpoint):
  ┌─ Beat every 5 min ──▶ bootstrap_realtime_tasks()
  │                         for each active endpoint without a live loop
  │                         (and each delayed one whose next_fetch_after passed)
  │                         └─ fetch_realtime_endpoint_task.apply_async(
  │                                (id, token), countdown=stagger)
  │
  │  fetch_realtime_endpoint_task(id, token)
  │    0. exit at once unless token holds the alive marker (or claims it
  │       when it is vacant)
  │    1. fetch the body
  │    2. store cached_file
  │    3. active: new token in the marker, .apply_async(countdown=interval)
  │       delayed/paused: clear the marker and stop; bootstrap resumes a
  │       delayed endpoint once next_fetch_after passes
  └─────────────────────────────────────────────────────────

Each endpoint has at most one effective loop. The alive marker
``rt-endpoint-alive:{id}`` holds the token of the one queued message allowed to
run next, and every run hands a fresh token to its successor. Any other copy
(a second seed after the marker expired under a lagging queue, a Redis
redelivery of an ETA message, a message left over from before a pause/resume)
carries a token that no longer matches and exits without fetching or
rescheduling. Back-off delays (up to 6 h) are never queued as ETAs, so no
message waits longer than the endpoint's interval.
"""

import os
import random
import time
import uuid

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.log import get_task_logger
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = get_task_logger(__name__)

# Light per-endpoint lock so that two runs for the same endpoint never overlap.
RT_ENDPOINT_LOCK_TTL_SECONDS = 300
# Spreads the start of endpoints after a bootstrap to avoid a burst.
RT_BOOTSTRAP_STAGGER_SECONDS = 3
RT_BOOTSTRAP_JITTER_MAX_SECONDS = 2
# Alive marker of a self-scheduling loop: holds the token of the next queued run
# and lives for that run's countdown plus this buffer. Once a loop dies and the
# marker expires, bootstrap may seed a new loop; a late message of the old loop
# then carries a stale token and exits.
RT_ALIVE_TTL_BUFFER_SECONDS = 120
# Marker value written before loop tokens existed; messages queued then carry no
# token. Lets those loops survive the deploy. Remove after the next release.
RT_LEGACY_ALIVE_MARKER = '1'
# Ile minut wstecz dispatcher statyczny nadrabia zgubione tiki Beata.
STATIC_DISPATCH_CATCHUP_MINUTES = 10
# One wall-clock budget for every download of a realtime verification (link
# checks, then first cache fetches): GBFS allows 9 endpoints at up to
# REALTIME_FETCH_TOTAL_TIMEOUT_SECONDS each, and the task runs on 'default'
# next to the dispatchers, so the total is capped rather than sized to the sum.
RT_VERIFICATION_BUDGET_SECONDS = 240
# Less than this left counts as none: a download clipped to a second or two
# would only time out and be recorded as a fetch failure of the endpoint.
RT_VERIFICATION_MIN_DOWNLOAD_SECONDS = 5
RT_VERIFICATION_SOFT_TIME_LIMIT = RT_VERIFICATION_BUDGET_SECONDS + 60
RT_VERIFICATION_TIME_LIMIT = RT_VERIFICATION_SOFT_TIME_LIMIT + 60
# Past the hard limit plus a queue backlog no run is in flight any more, so
# retry_stuck_realtime_verifications re-queues the submission.
RT_VERIFICATION_STUCK_AFTER_SECONDS = RT_VERIFICATION_TIME_LIMIT + 600


def enqueue(task, *args, **kwargs) -> bool:
    """Queue a Celery task without letting broker trouble break the request.

    ``.delay()`` is a network call to the broker. When Redis is down or
    misconfigured it raises, and because these calls happen inside request
    handling (upload signals, submission create/update) the whole API call
    used to fail with HTTP 500 — even though the submission itself had been
    saved successfully. Losing the queued job is recoverable (the periodic
    dispatchers re-seed realtime loops; stuck static validations are picked up
    by ``manage.py retry_gtfs_validation``, stuck realtime verifications by
    ``manage.py retry_realtime_validation``); failing the user's upload is not.

    Returns True when the task was queued. Logs an error otherwise.
    """
    try:
        task.delay(*args, **kwargs)
        return True
    except Exception:  # noqa: BLE001 - any broker/transport failure
        # Broad on purpose: kombu/redis raise a wide range of transport errors
        # and none of them should turn a successful write into a 500.
        logger.exception(
            'Could not queue %s(%s) — broker unreachable? The task was NOT '
            'scheduled; use manage.py retry_gtfs_validation (static) or wait '
            'for the periodic bootstrap (realtime).',
            getattr(task, 'name', task),
            ', '.join(map(repr, args)),
        )
        return False


def enqueue_on_commit(task, *args, **kwargs) -> None:
    """enqueue() once the current transaction commits, so the worker never reads
    uncommitted rows (runs at once outside a transaction)."""
    transaction.on_commit(lambda: enqueue(task, *args, **kwargs))


def _rt_alive_key(endpoint_id: int) -> str:
    return f'rt-endpoint-alive:{endpoint_id}'


def _reserve_rt_loop(endpoint_id: int, ttl: int) -> str | None:
    """Atomically claim the endpoint's loop: the token the seeded message must
    carry, or None when a live loop already holds the marker."""
    token = uuid.uuid4().hex
    return token if cache.add(_rt_alive_key(endpoint_id), token, timeout=ttl) else None


def schedule_realtime_endpoint_fetches(submission_id: int) -> int:
    """
    Queue an immediate fetch for hide_original endpoints of a published submission.
    Returns the number of newly scheduled loops. Request code calls it from
    transaction.on_commit, so the worker sees the committed endpoints.
    """
    from data_manager.models import (
        FETCH_STATUS_ACTIVE,
        RealtimeEndpointRT,
        completed_realtime_submission_ids,
    )

    if submission_id not in completed_realtime_submission_ids():
        return 0

    scheduled = 0
    for endpoint_id, interval in RealtimeEndpointRT.objects.filter(
        submission_id=submission_id,
        hide_original=True,
        fetch_status=FETCH_STATUS_ACTIVE,
    ).values_list('id', 'interval'):
        interval = interval or 60
        token = _reserve_rt_loop(endpoint_id, interval + RT_ALIVE_TTL_BUFFER_SECONDS)
        if token is None:
            continue
        # Called from requests: a broker outage must not fail the write, nor hold
        # it up once per endpoint. Release the reservation and stop; the periodic
        # bootstrap seeds this loop and the remaining (never reserved) ones.
        if not enqueue(fetch_realtime_endpoint_task, endpoint_id, token):
            cache.delete(_rt_alive_key(endpoint_id))
            break
        scheduled += 1
    return scheduled


def _rt_token_is_current(endpoint_id: int, token: str | None) -> bool:
    """Whether this message is the one the endpoint's loop is waiting for.

    A message that outlived its marker (queue lag, worker restart) re-claims the
    vacant slot instead of leaving the endpoint idle until the next bootstrap;
    it loses only to a loop that has claimed the slot meanwhile.
    """
    token = token or RT_LEGACY_ALIVE_MARKER
    key = _rt_alive_key(endpoint_id)
    return cache.get(key) == token or cache.add(key, token, timeout=RT_ENDPOINT_LOCK_TTL_SECONDS)


def _scheduler():
    """Leniwy import — unika circular imports przy starcie Celery."""
    from data_manager.scheduler import (
        _completed_realtime_submission_ids,
        _completed_submission_ids,
        _fetch_realtime_endpoint_rt,
        _fetch_static_entry,
    )

    return (
        _fetch_static_entry,
        _fetch_realtime_endpoint_rt,
        _completed_submission_ids,
        _completed_realtime_submission_ids,
    )


# ---------------------------------------------------------------------------
# STATYCZNE — dispatch co minutę przez Beat
# ---------------------------------------------------------------------------


@shared_task(name='data_manager.refresh_static_feeds')
def refresh_static_feeds_task() -> dict:
    """Run by Beat every minute; queue a fetch per entry that is due.

    An entry is due when download_time_1/2 falls in the catch-up window and no
    fetch has succeeded in it, or when its delayed retry time has passed. One
    DB query plus one .delay() per entry.
    """
    from datetime import timedelta

    from django.db.models import Q

    from data_manager.models import FETCH_STATUS_ACTIVE, FETCH_STATUS_DELAYED, StaticFeedEntry

    _, _, _completed, _ = _scheduler()

    # One clock read: two calls could straddle a minute boundary and dispatch
    # against a different minute than the one being logged.
    now = timezone.now()
    now_time = now.time().replace(second=0, microsecond=0)
    completed_ids = _completed()

    # Catch-up window. Matching download_time == this exact minute meant a Beat
    # tick lost to a restart, clock skew or a backed-up queue silently skipped
    # that day's download until tomorrow. Look back a few minutes instead, and
    # use last_fetch_success_at to keep the window from re-dispatching an entry
    # that already succeeded.
    window_start = now - timedelta(minutes=STATIC_DISPATCH_CATCHUP_MINUTES)
    window_times = [
        (now - timedelta(minutes=offset)).time().replace(second=0, microsecond=0)
        for offset in range(STATIC_DISPATCH_CATCHUP_MINUTES + 1)
    ]

    scheduled_due = (
        Q(fetch_status=FETCH_STATUS_ACTIVE)
        & (Q(download_time_1__in=window_times) | Q(download_time_2__in=window_times))
        & (Q(last_fetch_success_at__isnull=True) | Q(last_fetch_success_at__lt=window_start))
    )
    retry_due = Q(fetch_status=FETCH_STATUS_DELAYED, next_fetch_after__lte=now)

    entry_ids = list(
        StaticFeedEntry.objects.filter(
            hide_original=True,
            submission_id__in=completed_ids,
        )
        .exclude(url='')
        .filter(scheduled_due | retry_due)
        .values_list('id', flat=True)
    )

    # The window re-queues an entry every tick until a fetch succeeds; the
    # cutoff turns the copies piled up behind a slow fetch into no-ops.
    for entry_id in entry_ids:
        fetch_static_entry_task.delay(entry_id, skip_if_fetched_since=window_start.isoformat())

    logger.info('Dispatched %d static feed tasks for %s', len(entry_ids), now_time)
    return {'dispatched': len(entry_ids), 'time': str(now_time)}


@shared_task(
    bind=True,
    name='data_manager.fetch_static_entry',
    queue='feeds',
    # Must exceed FEED_FETCH_TOTAL_TIMEOUT_SECONDS (600). Under the global
    # 90 s default a slow 200 MB download was killed mid-fetch, so
    # mark_fetch_failure() never ran: no FeedFetchError, no retry schedule,
    # and the feed quietly stopped refreshing.
    soft_time_limit=660,
    time_limit=720,
)
def fetch_static_entry_task(self, entry_id: int, skip_if_fetched_since: str | None = None) -> dict:
    """Fetch and cache one StaticFeedEntry.

    Celery does not retry. A failed fetch stores a pause schedule on the model
    (5 min -> 1 h -> 6 h -> auto_paused) that the periodic dispatcher honours.

    ``skip_if_fetched_since`` (ISO instant, set by the dispatcher) makes a
    duplicate a no-op when a fetch has succeeded since then. Direct callers
    (a changed URL) omit it and always fetch.
    """
    from datetime import datetime

    from data_manager.models import StaticFeedEntry

    _fetch, _, _, _ = _scheduler()

    try:
        entry = StaticFeedEntry.objects.get(pk=entry_id)
    except StaticFeedEntry.DoesNotExist:
        logger.warning('StaticFeedEntry id=%d not found, skipping', entry_id)
        return {'status': 'not_found', 'entry_id': entry_id}

    if not entry.is_proxy_managed:
        logger.info('StaticFeedEntry id=%d is not proxy-managed, skipping fetch', entry_id)
        return {'status': 'skipped_not_proxied', 'entry_id': entry_id}

    if (
        skip_if_fetched_since
        and entry.last_fetch_success_at
        and entry.last_fetch_success_at >= datetime.fromisoformat(skip_if_fetched_since)
    ):
        return {'status': 'already_fetched', 'entry_id': entry_id}

    if not entry.fetch_is_due():
        return {
            'status': 'deferred',
            'entry_id': entry_id,
            'next_fetch_after': entry.next_fetch_after,
        }
    result = _fetch(entry)
    entry.refresh_from_db(fields=['fetch_status', 'next_fetch_after', 'fetch_failure_count'])
    return {
        'status': result,
        'entry_id': entry_id,
        'fetch_status': entry.fetch_status,
        'fetch_failure_count': entry.fetch_failure_count,
        'next_fetch_after': entry.next_fetch_after,
    }


# ---------------------------------------------------------------------------
# REALTIME — self-scheduling, no polling
# ---------------------------------------------------------------------------


@shared_task(name='data_manager.bootstrap_realtime_tasks')
def bootstrap_realtime_tasks() -> dict:
    """
    Run by Beat every few minutes as self-healing: seeds a self-scheduling loop
    for every active realtime endpoint without a live loop (alive marker), and
    for every delayed one whose next_fetch_after has passed.
    """
    from django.db.models import Q

    from data_manager.models import FETCH_STATUS_ACTIVE, FETCH_STATUS_DELAYED, RealtimeEndpointRT

    _, _, _, _completed_rt = _scheduler()

    completed_ids = _completed_rt()
    endpoints = list(
        RealtimeEndpointRT.objects.filter(
            hide_original=True,
            submission_id__in=completed_ids,
        )
        .filter(
            Q(fetch_status=FETCH_STATUS_ACTIVE)
            | Q(fetch_status=FETCH_STATUS_DELAYED, next_fetch_after__lte=timezone.now())
        )
        .values_list('id', 'interval')
    )

    scheduled = 0
    for index, (endpoint_id, interval) in enumerate(endpoints):
        interval = interval or 60
        # Spread the start of endpoints to avoid a simultaneous burst.
        stagger = (index * RT_BOOTSTRAP_STAGGER_SECONDS) + random.randint(
            0, RT_BOOTSTRAP_JITTER_MAX_SECONDS
        )
        token = _reserve_rt_loop(endpoint_id, stagger + interval + RT_ALIVE_TTL_BUFFER_SECONDS)
        if token is None:
            continue
        fetch_realtime_endpoint_task.apply_async(
            args=[endpoint_id, token],
            countdown=stagger,
        )
        scheduled += 1

    logger.info(
        'Bootstrapped %d realtime endpoint tasks (%d already alive)',
        scheduled,
        len(endpoints) - scheduled,
    )
    return {'bootstrapped': scheduled, 'skipped_alive': len(endpoints) - scheduled}


@shared_task(
    name='data_manager.fetch_realtime_endpoint',
)
def fetch_realtime_endpoint_task(endpoint_id: int, token: str | None = None) -> dict:
    """
    Fetch the cache for one RealtimeEndpointRT, then schedule itself again in
    `interval` seconds (self-scheduling loop).

    `token` must match the endpoint's alive marker; otherwise this message is a
    stale copy of the loop and exits without fetching or rescheduling.

    Network errors are recorded as FeedFetchError. The loop follows the stored
    fetch state: the interval after success, stop on a delay or a pause.
    """
    from data_manager.models import (
        FETCH_STATUS_ACTIVE,
        RealtimeEndpointRT,
    )

    if not _rt_token_is_current(endpoint_id, token):
        logger.info('RealtimeEndpointRT id=%d stale loop token, exiting', endpoint_id)
        return {'status': 'stale', 'endpoint_id': endpoint_id}

    _, _fetch, _, _completed_rt = _scheduler()

    lock_key = f'rt-endpoint-lock:{endpoint_id}'
    # cache.add returns False when the lock already exists.
    lock_acquired = cache.add(lock_key, '1', timeout=RT_ENDPOINT_LOCK_TTL_SECONDS)
    if not lock_acquired:
        logger.info('RealtimeEndpointRT id=%d already running, skipping overlap', endpoint_id)
        return {'status': 'skipped_overlap', 'endpoint_id': endpoint_id}

    try:
        try:
            endpoint = RealtimeEndpointRT.objects.select_related('submission').get(pk=endpoint_id)
        except RealtimeEndpointRT.DoesNotExist:
            logger.info('RealtimeEndpointRT id=%d deleted, stopping loop', endpoint_id)
            cache.delete(_rt_alive_key(endpoint_id))
            return {'status': 'stopped', 'endpoint_id': endpoint_id}

        completed_ids = set(_completed_rt())
        if endpoint.submission_id not in completed_ids:
            logger.info(
                'RealtimeEndpointRT id=%d realtime submission no longer published, stopping loop',
                endpoint_id,
            )
            cache.delete(_rt_alive_key(endpoint_id))
            return {'status': 'stopped', 'endpoint_id': endpoint_id}

        if not endpoint.is_proxy_managed:
            logger.info(
                'RealtimeEndpointRT id=%d is not proxy-managed, stopping loop',
                endpoint_id,
            )
            cache.delete(_rt_alive_key(endpoint_id))
            return {'status': 'skipped_not_proxied', 'endpoint_id': endpoint_id}

        now = timezone.now()
        if not endpoint.fetch_is_due(now):
            logger.info(
                'RealtimeEndpointRT id=%d fetch status=%s, stopping loop',
                endpoint_id,
                endpoint.fetch_status,
            )
            cache.delete(_rt_alive_key(endpoint_id))
            return {
                'status': endpoint.fetch_status,
                'endpoint_id': endpoint_id,
                'next_fetch_after': endpoint.next_fetch_after,
            }

        try:
            result = _fetch(endpoint, now)
        except Exception as exc:
            logger.warning('fetch_realtime_endpoint_rt id=%d error: %s', endpoint_id, exc)
            result = 'error'

        endpoint.refresh_from_db(
            fields=[
                'fetch_status',
                'next_fetch_after',
                'fetch_failure_count',
                'interval',
            ]
        )
        outcome = {
            'status': result,
            'endpoint_id': endpoint_id,
            'fetch_status': endpoint.fetch_status,
            'fetch_failure_count': endpoint.fetch_failure_count,
            'next_fetch_after': endpoint.next_fetch_after,
        }
        if endpoint.fetch_status != FETCH_STATUS_ACTIVE:
            # Never queue the back-off: Redis redelivers ETAs past its 1 h
            # visibility timeout. Bootstrap resumes the endpoint instead.
            cache.delete(_rt_alive_key(endpoint_id))
            return outcome

        countdown = endpoint.interval
        next_token = uuid.uuid4().hex
        cache.set(
            _rt_alive_key(endpoint_id),
            next_token,
            timeout=countdown + RT_ALIVE_TTL_BUFFER_SECONDS,
        )
        fetch_realtime_endpoint_task.apply_async(
            args=[endpoint_id, next_token], countdown=countdown
        )
        return {**outcome, 'next_in': countdown}
    finally:
        cache.delete(lock_key)


# ---------------------------------------------------------------------------
# GTFS VALIDATION TASK
# ---------------------------------------------------------------------------


@shared_task(
    name='data_manager.validate_gtfs_feed',
    bind=True,
    queue='feeds',
    soft_time_limit=600,
    time_limit=660,
)
def validate_gtfs_feed_task(self, entry_id: int):
    """
    Validates a static GTFS feed using the image in settings.GTFS_VALIDATOR_IMAGE.

    Steps:
    1. Identify the input file (user upload 'file' OR server download 'cached_file').
    2. Prepare temporary output directory on HOST (accessible via bind mount).
    3. Run Docker container (DinD) with input/output mounts.
    4. Parse report.json.
    5. Update FeedValidationReport and Submission stage.
    """
    import json
    import shutil

    import docker
    from celery.exceptions import SoftTimeLimitExceeded
    from django.conf import settings

    from data_manager import validation_errors as ve
    from data_manager.models import (
        FeedSubmission,
        FeedSubmissionHistory,
        FeedValidationReport,
        StaticFeedEntry,
    )

    logger.info(f"Starting GTFS validation for StaticFeedEntry id={entry_id}")

    try:
        entry = StaticFeedEntry.objects.select_related('submission').get(id=entry_id)
    except StaticFeedEntry.DoesNotExist:
        logger.warning(f"StaticFeedEntry {entry_id} not found. Aborting validation.")
        return

    if entry.submission.data_type != 'gtfs':
        logger.info(
            'Skipping GTFS validation for StaticFeedEntry %s (data_type=%s)',
            entry_id,
            entry.submission.data_type,
        )
        return

    def _set_validation_status(status: str, message: str = '') -> None:
        entry.validation_status = status
        entry.validation_message = message
        entry.save(update_fields=['validation_status', 'validation_message'])

    _set_validation_status(StaticFeedEntry.VALIDATION_PENDING)

    def _lock_submission() -> FeedSubmission:
        # Overlapping runs (upload signal, proxy refresh, retry command) must read
        # the stage one at a time: read it from the returned row, in the same atomic block.
        return (
            FeedSubmission.objects.select_for_update()
            .prefetch_related('history')
            .get(pk=entry.submission_id)
        )

    def _record_infra_error(code: str, detail) -> None:
        # Validation could not run (our side, not the feed): no rejection, the
        # stage stays as it is and manage.py retry_gtfs_validation re-runs it.
        # The provider sees only the code; the details go to the server log.
        _set_validation_status(StaticFeedEntry.VALIDATION_ERROR, code)
        logger.error(
            'GTFS validation of StaticFeedEntry %s (submission %s) could not run: %s: %s',
            entry.id,
            entry.submission_id,
            code,
            detail,
        )

    def _reject_submission(reason: str) -> None:
        _set_validation_status(StaticFeedEntry.VALIDATION_INVALID, reason)
        with transaction.atomic():
            submission = _lock_submission()
            stage = submission.current_stage
            if stage >= 4:
                logger.warning(
                    'GTFS validation failed for published submission %s; not reverting stage (was %s): %s',
                    submission.id,
                    stage,
                    reason,
                )
                return
            if submission.rejection_cause == reason:
                # The same verdict is already recorded, e.g. by an overlapping run.
                return
            FeedSubmissionHistory.objects.create(
                submission=submission,
                event_type=FeedSubmissionHistory.EVENT_REJECTED,
                stage_before=stage,
                stage_after=1,
                cause=reason,
                actor=None,
            )
        logger.warning(f"Submission {submission.id} rejected: {reason}")

    # Both work dirs carry a random suffix, so a leaked one is never reused and
    # repeated failures would grow the disk without bound. Declared here, before
    # the first statement that can raise, and reclaimed by the finally at the
    # end — which also covers SoftTimeLimitExceeded, the case that used to kill
    # this task mid-run and leak the directory (25 orphans on disk).
    # rmtree(ignore_errors=True) is a no-op for dirs that were never created.
    validation_temp_dir = None
    container_output_dir = None

    def _cleanup_workdirs() -> None:
        # Closures read the CURRENT binding, so both names resolve to whatever
        # the body below assigned by the time this runs.
        if container_output_dir:
            shutil.rmtree(container_output_dir, ignore_errors=True)
        if validation_temp_dir:
            shutil.rmtree(validation_temp_dir, ignore_errors=True)

    try:
        # 1. Determine which file to validate
        # Prefer cached_file if present (latest proxy download), else user file.
        # For non-proxy URL feeds, download once to a temp dir for validation only.
        file_field = entry.cached_file if entry.cached_file else entry.file

        if not file_field and entry.url:
            from urllib.parse import urlparse

            import requests

            from data_manager.net_security import OutboundURLBlocked, safe_download
            from data_manager.scheduler import _build_auth_headers

            validation_temp_dir = os.path.join(
                settings.MEDIA_ROOT,
                str(entry.submission_id),
                'validation',
                f'tmp_{entry.id}_{random.randint(1000, 9999)}',
            )
            os.makedirs(validation_temp_dir, exist_ok=True)
            try:
                parsed = urlparse(entry.url)
                filename = os.path.basename(parsed.path) or 'feed.zip'
                container_file_path = os.path.join(validation_temp_dir, filename)
                # Copied chunk by chunk: a 200 MB archive must not be held in
                # memory just to be written straight back out to disk.
                with safe_download(
                    entry.url,
                    headers=_build_auth_headers(entry.auth_type, entry.auth_value),
                    timeout=60,
                    max_bytes=settings.MAX_FEED_FILE_SIZE_BYTES,
                    total_timeout=settings.FEED_FETCH_TOTAL_TIMEOUT_SECONDS,
                ) as (response, body):
                    response.raise_for_status()
                    with open(container_file_path, 'wb') as fh:
                        shutil.copyfileobj(body, fh)
                container_input_dir = validation_temp_dir
            except OutboundURLBlocked as exc:
                # The provider's URL, not our infrastructure: rejected as before.
                _reject_submission(str(exc))
                return
            except requests.exceptions.RequestException as exc:
                _reject_submission(f'Failed to download feed for validation: {exc}')
                return
        elif not file_field:
            _record_infra_error(ve.FILE_MISSING, 'no file, cached file or URL')
            return
        else:
            # file_field.name is relative to MEDIA_ROOT (e.g. '1/2/static/feed.zip')
            relative_path = file_field.name
            if relative_path.startswith('uploaded_data/'):
                relative_path = relative_path[len('uploaded_data/') :]
            filename = os.path.basename(relative_path)
            container_file_path = file_field.path
            container_input_dir = os.path.dirname(container_file_path)

        relative_dir = os.path.relpath(container_input_dir, settings.MEDIA_ROOT)

        # Host paths for Docker bind mounts
        host_input_dir = os.path.join(settings.HOST_MEDIA_ROOT, relative_dir)

        report_dir_name = f"validation_report_{entry.id}_{random.randint(1000, 9999)}"
        container_output_dir = os.path.join(container_input_dir, report_dir_name)
        host_output_dir = os.path.join(host_input_dir, report_dir_name)

        if not os.path.exists(container_file_path):
            _record_infra_error(ve.FILE_MISSING, f'not on disk: {container_file_path}')
            return

        # NOTE: Do not check that the file exists at the host path here.
        # This code runs inside the worker container and does not have access
        # to host filesystem paths like /home/jakub/..., which causes false rejections.

        # Prepare output directory
        try:
            # Create the container-visible output dir (project bind mount), which also
            # materializes the directory on the host via the /app volume.
            os.makedirs(container_output_dir, exist_ok=True)
            try:
                # Group-writable so the validator container (same UID:GID) can write,
                # but not world-writable.
                os.chmod(container_output_dir, 0o770)
            except Exception:
                pass
        except OSError as e:
            _record_infra_error(ve.VALIDATOR_FAILED, f'cannot create the output dir: {e}')
            return

        logger.info(f"Validating {filename}...")
        logger.info(f"Host Input: {host_input_dir}, Host Output: {host_output_dir}")

        try:
            client = docker.from_env()
        except docker.errors.DockerException as e:
            _record_infra_error(
                ve.VALIDATOR_UNAVAILABLE,
                f'cannot connect to Docker (check docker-socket-proxy / DOCKER_HOST): {e}',
            )
            return

        try:
            volumes = {
                host_input_dir: {'bind': '/input', 'mode': 'ro'},
                host_output_dir: {'bind': '/output', 'mode': 'rw'},
            }

            command = ["-i", f"/input/{filename}", "-o", "/output"]

            # Bound resource usage so a malicious/zip-bomb feed cannot exhaust the host.
            run_kwargs = dict(
                image=settings.GTFS_VALIDATOR_IMAGE,
                command=command,
                volumes=volumes,
                remove=True,
                detach=False,
                user=f"{settings.GTFS_VALIDATOR_UID}:{settings.GTFS_VALIDATOR_GID}",
                network_disabled=True,
                mem_limit=settings.GTFS_VALIDATOR_MEM_LIMIT,
                pids_limit=settings.GTFS_VALIDATOR_PIDS_LIMIT,
                nano_cpus=settings.GTFS_VALIDATOR_NANO_CPUS,
                cap_drop=['ALL'],
                security_opt=['no-new-privileges:true'],
            )

            client.containers.run(**run_kwargs)

            logger.info("Validation container finished.")

        except docker.errors.ContainerError as e:
            # Non-zero exit, e.g. 137 when the memory limit killed it.
            _record_infra_error(ve.VALIDATOR_FAILED, f'container failed: {e}')
            return
        except SoftTimeLimitExceeded:
            raise
        except Exception as e:
            _record_infra_error(ve.VALIDATOR_UNAVAILABLE, f'Docker execution error: {e}')
            return

        # Parse results
        # The validator writes files into the host_output_dir (bound to /output in the
        # validator container). Read report.json from the container-visible path that
        # mirrors the host directory via the /app bind mount.
        report_file = os.path.join(container_output_dir, 'report.json')

        # Add a small retry loop to handle potential filesystem delays with Docker volumes
        max_retries = 5
        retry_delay = 1  # seconds
        for attempt in range(max_retries):
            if os.path.exists(report_file):
                break
            logger.warning(
                "report.json not found at %s (attempt %d). Retrying in %ds...",
                report_file,
                attempt + 1,
                retry_delay,
            )
            time.sleep(retry_delay)
        else:
            _record_infra_error(
                ve.VALIDATOR_FAILED,
                f'report.json not found after {max_retries} retries: {container_output_dir}',
            )
            return

        try:
            with open(report_file, encoding='utf-8') as f:
                report_data = json.load(f)
        except json.JSONDecodeError as e:
            _record_infra_error(ve.VALIDATOR_FAILED, f'cannot decode report.json: {e}')
            return

        def extract_notice_counts(payload: dict) -> tuple[int, int, dict]:
            """Parse a GTFS Validator report payload and return (error_count, warning_count, error_code_counts).

            This mirrors the original nested function used inside validate_gtfs_feed_task.
            """
            summary = payload.get('summary', {}) if isinstance(payload, dict) else {}

            # Summary-based fallbacks
            error_fallback = (
                summary.get('errorCount')
                or summary.get('error_count')
                or summary.get('errors')
                or 0
            )
            warning_fallback = (
                summary.get('warningCount')
                or summary.get('warning_count')
                or summary.get('warnings')
                or 0
            )

            # Look for notices lists in known fields
            candidates = []
            for key in ('notices', 'results', 'noticeResults', 'validationResults'):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates.append(value)
            notices = candidates[0] if candidates else []

            error_count = 0
            warning_count = 0
            error_code_counts = {}
            if isinstance(notices, list):
                for notice in notices:
                    if not isinstance(notice, dict):
                        continue
                    severity = str(notice.get('severity', '')).upper()
                    total = int(notice.get('totalNotices', 0) or 0)
                    code = str(notice.get('code', '')).strip()
                    if severity == 'ERROR':
                        error_count += max(1, total)
                        if code:
                            error_code_counts[code] = error_code_counts.get(code, 0) + max(1, total)
                    elif severity == 'WARNING':
                        warning_count += max(1, total)

            if error_count == 0 and warning_count == 0:
                # Fallback to summary counts when notices are missing
                error_count = int(error_fallback or 0)
                warning_count = int(warning_fallback or 0)

            return error_count, warning_count, error_code_counts

        error_count, warning_count, error_code_counts = extract_notice_counts(report_data)

        # Create/Update ValidationReport
        # The FeedValidationReport model is not linked from the report side; StaticFeedEntry
        # holds a OneToOneField `validation_report`. Update existing report if present,
        # otherwise create a new FeedValidationReport and attach it to the entry.
        from django.core.files.base import ContentFile

        report_bytes = json.dumps(report_data, ensure_ascii=False).encode('utf-8')
        if entry.validation_report_id:
            val_report = entry.validation_report
            val_report.error_count = error_count
            val_report.warning_count = warning_count
            val_report.report_file.save('report.json', ContentFile(report_bytes))
            val_report.save()
        else:
            val_report = FeedValidationReport.objects.create(
                error_count=error_count,
                warning_count=warning_count,
            )
            # Link the report to its entry BEFORE saving the file so that
            # validation_file_path can resolve the submission id (otherwise the file
            # would land in 'unknown/validation/' and collide across submissions).
            entry.validation_report = val_report
            entry.save(update_fields=['validation_report'])
            val_report.report_file.save('report.json', ContentFile(report_bytes))

        # Update Submission Stage
        if error_count > 0:
            # Build rejection_cause as "code: count" list
            if error_code_counts:
                parts = [f"{code}: {count}" for code, count in sorted(error_code_counts.items())]
                cause = ", ".join(parts)
            else:
                cause = f"Validation failed with {error_count} errors."
            _reject_submission(cause)
        else:
            _set_validation_status(StaticFeedEntry.VALIDATION_VALID)
            with transaction.atomic():
                submission = _lock_submission()
                stage = submission.current_stage
                # Already at stage 3 or later, possibly by an overlapping run.
                if stage < 3:
                    FeedSubmissionHistory.objects.create(
                        submission=submission,
                        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
                        stage_before=stage,
                        stage_after=3,
                        actor=None,
                    )
                    logger.info(
                        f"Submission {submission.id} advanced to Stage 3 (valid GTFS, awaiting admin)."
                    )
    except SoftTimeLimitExceeded:
        _record_infra_error(ve.TIMEOUT, 'soft time limit exceeded')
    finally:
        _cleanup_workdirs()


@shared_task(
    name='data_manager.validate_realtime_submission',
    soft_time_limit=RT_VERIFICATION_SOFT_TIME_LIMIT,
    time_limit=RT_VERIFICATION_TIME_LIMIT,
)
def validate_realtime_submission_task(submission_id: int) -> dict:
    try:
        return _verify_realtime_submission(submission_id)
    except SoftTimeLimitExceeded:
        # Downloads fit RT_VERIFICATION_BUDGET_SECONDS and slow endpoints end
        # as endpoint_timeout, so the time went elsewhere (worker, database,
        # storage): an infrastructure error, not a rejection. Stay at stage 2
        # for retry_stuck_realtime_verifications.
        logger.error(
            'Realtime verification of submission %s hit the soft time limit (%ss); '
            'kept at stage 2 for a retry',
            submission_id,
            RT_VERIFICATION_SOFT_TIME_LIMIT,
        )
        return {'status': 'timed_out', 'id': submission_id}


def _verify_realtime_submission(submission_id: int) -> dict:
    """
    Realtime verification (stage 2): link-check every endpoint, validate the
    response format, and do the first cache fetch for hide_original endpoints.

    Flow: stage 1 -> 2 (verification starts) -> 3 on success; on failure the
    submission is rejected back to stage 1 with coded reasons
    (data_manager/rejection_reasons.py) that the frontend translates. A
    published submission (stage 4) is never rejected: the failure is logged.

    Historical note: this used to run "semantic validation" in a
    gtfs-realtime-validator container. It registered feeds with the validator
    and never read the results, so it could not detect anything, while the
    container (a JVM with no memory limit, ~3.9 GB heap) ran non-stop. Removed.
    GTFS-RT format is now actually checked: the protobuf is parsed in
    validate_realtime_feed_content.
    """
    import requests

    from data_manager import rejection_reasons as rr
    from data_manager.models import (
        RealtimeSubmission,
        RealtimeSubmissionHistory,
        completed_submission_ids,
    )
    from data_manager.net_security import OutboundURLBlocked, ResponseTooLarge, safe_get
    from data_manager.scheduler import _build_auth_headers, _fetch_realtime_endpoint_rt
    from data_manager.validators import (
        EmptyFeedContent,
        InvalidFeedContent,
        validate_realtime_feed_content,
    )

    max_link_bytes = settings.MAX_REALTIME_FEED_BYTES

    try:
        rts = (
            RealtimeSubmission.objects.select_related('static_submission')
            .prefetch_related('endpoints')
            .get(pk=submission_id)
        )
    except RealtimeSubmission.DoesNotExist:
        return {'status': 'not_found', 'id': submission_id}

    def _lock_submission() -> RealtimeSubmission:
        # Overlapping runs (create, resubmission, retry command) must read the
        # stage one at a time: read it from the returned row, in the same atomic block.
        return (
            RealtimeSubmission.objects.select_for_update()
            .prefetch_related('history')
            .get(pk=submission_id)
        )

    def _reject(reasons: list[dict]) -> dict:
        with transaction.atomic():
            submission = _lock_submission()
            stage = submission.current_stage
            if stage >= 4:
                # A failed revalidation of a published feed must not unpublish it
                # (ADR 0005), same as the static path: report it and keep the stage.
                logger.warning(
                    'Realtime validation failed for published submission %s; not reverting '
                    'stage (was %s): %s',
                    submission_id,
                    stage,
                    ', '.join(reason['code'] for reason in reasons),
                )
                return {'status': 'failed_published', 'reasons': reasons}
            # Unless the same verdict is already recorded, e.g. by an overlapping run.
            if submission.rejection_reasons != reasons:
                RealtimeSubmissionHistory.objects.create(
                    submission=submission,
                    event_type=RealtimeSubmissionHistory.EVENT_REJECTED,
                    stage_before=stage,
                    stage_after=1,
                    reasons=reasons,
                    actor=None,
                )
        return {'status': 'rejected', 'reasons': reasons}

    def _advance(stage_after: int) -> None:
        with transaction.atomic():
            submission = _lock_submission()
            stage = submission.current_stage
            if stage >= stage_after:
                # Already moved, by an overlapping run or a reviewer.
                return
            RealtimeSubmissionHistory.objects.create(
                submission=submission,
                event_type=RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
                stage_before=stage,
                stage_after=stage_after,
                actor=None,
            )

    _advance(2)

    if rts.protocol in (RealtimeSubmission.PROTOCOL_GTFS_RT, RealtimeSubmission.PROTOCOL_SIRI):
        if not rts.static_submission_id:
            return _reject([rr.rejection_reason(rr.STATIC_SUBMISSION_MISSING)])
        if rts.static_submission_id not in completed_submission_ids():
            return _reject([rr.rejection_reason(rr.STATIC_SUBMISSION_NOT_PUBLISHED)])

    endpoints = list(rts.endpoints.all())
    if not endpoints:
        return _reject([rr.rejection_reason(rr.NO_ENDPOINTS)])

    reasons: list[dict] = []

    # Link check every endpoint: reachable, and does the body actually look
    # like the declared protocol? Capped at MAX_REALTIME_FEED_BYTES — realtime
    # payloads are small, and a status code never needed a 200 MB budget.
    # Most specific first: ConnectTimeout is both a Timeout and a ConnectionError.
    exception_codes = (
        (EmptyFeedContent, rr.ENDPOINT_CONTENT_EMPTY),
        (InvalidFeedContent, rr.ENDPOINT_CONTENT_INVALID),
        (ResponseTooLarge, rr.ENDPOINT_TOO_LARGE),
        (OutboundURLBlocked, rr.ENDPOINT_URL_BLOCKED),
        (requests.exceptions.Timeout, rr.ENDPOINT_TIMEOUT),
        (requests.exceptions.ConnectionError, rr.ENDPOINT_UNREACHABLE),
    )

    deadline = time.monotonic() + RT_VERIFICATION_BUDGET_SECONDS

    def _download_budget() -> float:
        return min(settings.REALTIME_FETCH_TOTAL_TIMEOUT_SECONDS, deadline - time.monotonic())

    for ep in endpoints:
        entity_types = set()
        try:
            budget = _download_budget()
            if budget < RT_VERIFICATION_MIN_DOWNLOAD_SECONDS:
                # The endpoints before this one used up the budget: too slow.
                raise requests.exceptions.Timeout('verification budget used up')
            resp = safe_get(
                ep.url,
                headers=_build_auth_headers(ep.auth_type, ep.auth_value),
                timeout=15,
                max_bytes=max_link_bytes,
                total_timeout=budget,
            )
            if resp.status_code >= 400:
                reasons.append(
                    rr.rejection_reason(
                        rr.ENDPOINT_HTTP_ERROR,
                        endpoint_type=ep.endpoint_type,
                        status=resp.status_code,
                    )
                )
            else:
                entity_types = validate_realtime_feed_content(rts.protocol, resp.content)
        except SoftTimeLimitExceeded:
            # Not the endpoint's fault: end the task as timed_out.
            raise
        except Exception as exc:
            code = next(
                (c for exc_type, c in exception_codes if isinstance(exc, exc_type)),
                rr.ENDPOINT_FETCH_FAILED,
            )
            # The code is what the user sees; the exception text is for the team.
            logger.warning(
                'Realtime validation of submission %s, endpoint_rt=%s: %s (%s)',
                submission_id,
                ep.pk,
                code,
                exc,
            )
            extra = {}
            if code == rr.ENDPOINT_CONTENT_INVALID:
                extra['protocol'] = rts.protocol
            elif code == rr.ENDPOINT_TOO_LARGE:
                extra['limit_bytes'] = exc.max_bytes
            reasons.append(rr.rejection_reason(code, endpoint_type=ep.endpoint_type, **extra))
        # Outside the try: saving them is not a fault of the endpoint.
        ep.record_entity_types(entity_types)

    if not reasons:
        now = timezone.now()
        for ep in endpoints:
            if not ep.hide_original:
                continue
            budget = _download_budget()
            if budget < RT_VERIFICATION_MIN_DOWNLOAD_SECONDS:
                reasons.append(
                    rr.rejection_reason(rr.ENDPOINT_TIMEOUT, endpoint_type=ep.endpoint_type)
                )
                continue
            _fetch_realtime_endpoint_rt(ep, now, total_timeout=budget)
            ep.refresh_from_db(fields=['cached_file', 'cached_at'])
            if not ep.cached_file:
                reasons.append(
                    rr.rejection_reason(rr.ENDPOINT_CACHE_FAILED, endpoint_type=ep.endpoint_type)
                )

    if reasons:
        return _reject(reasons)

    _advance(3)
    return {'status': 'ok', 'id': submission_id}


@shared_task(name='data_manager.retry_stuck_realtime_verifications')
def retry_stuck_realtime_verifications() -> dict:
    """Beat: re-queue verifications whose latest history entry is their own
    advance to stage 2 (actor None) older than RT_VERIFICATION_STUCK_AFTER_SECONDS.
    A stage 2 set by a reviewer is left alone.
    """
    from datetime import timedelta

    from django.db.models import OuterRef, Subquery

    from data_manager.models import RealtimeSubmissionHistory

    cutoff = timezone.now() - timedelta(seconds=RT_VERIFICATION_STUCK_AFTER_SECONDS)
    latest_entry = (
        RealtimeSubmissionHistory.objects.filter(submission=OuterRef('submission'))
        .order_by('-created_at', '-id')
        .values('id')[:1]
    )
    submission_ids = list(
        RealtimeSubmissionHistory.objects.filter(
            id=Subquery(latest_entry),
            stage_after=2,
            actor__isnull=True,
            created_at__lte=cutoff,
        ).values_list('submission_id', flat=True)
    )

    queued = 0
    for submission_id in submission_ids:
        # Re-queuing does not move the stage-2 timestamp, so without this marker
        # every sweep would queue another run while one is still in progress.
        marker = f'rt-verification-requeued:{submission_id}'
        if not cache.add(marker, '1', timeout=RT_VERIFICATION_STUCK_AFTER_SECONDS):
            continue
        if not enqueue(validate_realtime_submission_task, submission_id):
            cache.delete(marker)
            break
        queued += 1

    if submission_ids:
        logger.warning(
            'Re-queued %d of %d realtime verifications stuck at stage 2',
            queued,
            len(submission_ids),
        )
    return {'stuck': len(submission_ids), 'queued': queued}
