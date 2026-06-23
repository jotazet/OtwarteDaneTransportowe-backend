"""
Celery tasks — pobieranie i odswiezanie cache feedow.

Architektura taskow:
─────────────────────────────────────────────────────────────
  STATYCZNE (download_time_1/2):
  ┌─ Beat co minute ──▶ refresh_static_feeds_task()
  │                        sprawdza download_time_1/2 == teraz
  │                        └─ .delay() ──▶ fetch_static_entry_task(id)
  └─────────────────────────────────────────────────────────

  REALTIME (interval per endpoint):
  ┌─ Beat raz przy starcie ──▶ bootstrap_realtime_tasks()
  │                              dla kazdego aktywnego endpointu
  │                              └─ fetch_realtime_endpoint_task.apply_async(
  │                                     countdown=stagger)  ← rozlozony start
  │
  │  fetch_realtime_endpoint_task(id)
  │    1. pobierz plik
  │    2. zapisz cached_file
  │    3. .apply_async(countdown=endpoint.interval)  ← zaplanuj siebie na pozniej
  └─────────────────────────────────────────────────────────

Zalety self-scheduling:
  • Brak pollingu DB co 10 s — Beat nie jest potrzebny dla RT po bootstrapie.
  • Kazdy endpoint respektuje swoj interval (np. 15 s, 30 s, 60 s).
  • Skalowalnosc — wiele endpointow moze pracowac niezaleznie.
"""

import random
import os
import time

from celery import shared_task
from celery.utils.log import get_task_logger
from django.core.cache import cache
from django.utils import timezone

logger = get_task_logger(__name__)

# Lekki lock per endpoint, aby unikac nakladania fetchy dla tego samego RT.
RT_ENDPOINT_LOCK_TTL_SECONDS = 300
# Ogranicza burst po starcie: endpointy sa uruchamiane z malym odstepem.
RT_BOOTSTRAP_STAGGER_SECONDS = 3
RT_BOOTSTRAP_JITTER_MAX_SECONDS = 2
# Marker "zywej" petli self-scheduling. Odswiezany przy kazdym zaplanowaniu
# kolejnego wykonania (TTL = interval + bufor). Gdy petla padnie i marker
# wygasnie, bootstrap moze ja ponownie zasiac — bez tworzenia duplikatow,
# dopoki marker istnieje.
RT_ALIVE_TTL_BUFFER_SECONDS = 120


def _rt_alive_key(endpoint_id: int) -> str:
    return f'rt-endpoint-alive:{endpoint_id}'


def _seconds_until(dt) -> int:
    return max(0, int((dt - timezone.now()).total_seconds()))


def schedule_realtime_endpoint_fetches(submission_id: int) -> int:
    """
    Queue an immediate fetch for hide_original endpoints of a published submission.
    Returns the number of newly scheduled loops.
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
        reserve_ttl = interval + RT_ALIVE_TTL_BUFFER_SECONDS
        if not cache.add(_rt_alive_key(endpoint_id), '1', timeout=reserve_ttl):
            continue
        fetch_realtime_endpoint_task.apply_async(args=[endpoint_id], countdown=0)
        scheduled += 1
    return scheduled


def _mark_rt_alive(endpoint_id: int, interval: int) -> None:
    cache.set(
        _rt_alive_key(endpoint_id),
        '1',
        timeout=int(interval) + RT_ALIVE_TTL_BUFFER_SECONDS,
    )


def _scheduler():
    """Leniwy import — unika circular imports przy starcie Celery."""
    from data_manager.scheduler import (
        _fetch_realtime_endpoint_rt,
        _fetch_static_entry,
        _completed_submission_ids,
        _completed_realtime_submission_ids,
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
    """
    Uruchamiany przez Beat co minutę (crontab).
    Sprawdza download_time_1/2 == teraz i kolejkuje fetch per wpis.
    Lekki — tylko jedno zapytanie do DB + N .delay() calls.
    """
    from data_manager.models import FETCH_STATUS_ACTIVE, FETCH_STATUS_DELAYED, StaticFeedEntry
    from django.db.models import Q
    _, _, _completed, _ = _scheduler()

    now = timezone.now()
    now_time = timezone.now().time().replace(second=0, microsecond=0)
    completed_ids = _completed()

    scheduled_due = (
        Q(fetch_status=FETCH_STATUS_ACTIVE)
        & (Q(download_time_1=now_time) | Q(download_time_2=now_time))
    )
    retry_due = Q(fetch_status=FETCH_STATUS_DELAYED, next_fetch_after__lte=now)

    entry_ids = list(
        StaticFeedEntry.objects.filter(
            hide_original=True,
            url__isnull=False,
            submission_id__in=completed_ids,
        ).filter(
            scheduled_due | retry_due
        ).values_list('id', flat=True)
    )

    for entry_id in entry_ids:
        fetch_static_entry_task.delay(entry_id)

    logger.info('Dispatched %d static feed tasks for %s', len(entry_ids), now_time)
    return {'dispatched': len(entry_ids), 'time': str(now_time)}


@shared_task(
    bind=True,
    name='data_manager.fetch_static_entry',
    queue='feeds',
)
def fetch_static_entry_task(self, entry_id: int) -> dict:
    """Pobiera i zapisuje cache dla jednego StaticFeedEntry.

    Retry nie jest robiony przez Celery. Nieudane pobrania ustawiaja utrwalony
    harmonogram pauz na modelu (5min -> 1h -> 6h -> auto_paused), ktory
    respektuje cykliczny dispatcher.
    """
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

    if not entry.fetch_is_due():
        return {'status': 'deferred', 'entry_id': entry_id, 'next_fetch_after': entry.next_fetch_after}
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
# REALTIME — self-scheduling, bez pollingu
# ---------------------------------------------------------------------------

@shared_task(name='data_manager.bootstrap_realtime_tasks')
def bootstrap_realtime_tasks() -> dict:
    """
    Uruchamiany cyklicznie przez Beat (co kilka minut) jako mechanizm
    samonaprawy: zasiewa petle self-scheduling dla aktywnych endpointow RT,
    ktore NIE maja jeszcze zywej petli.

    Deduplikacja: dla kazdego endpointu istnieje marker `rt-endpoint-alive:{id}`
    odswiezany przy kazdym zaplanowaniu kolejnego wykonania. Bootstrap pomija
    endpointy z aktywnym markerem, dzieki czemu nie mnozy rownoleglych petli.
    `cache.add` rezerwuje slot atomowo (odporne na rownolegle bootstrapy).
    """
    from data_manager.models import FETCH_STATUS_ACTIVE, FETCH_STATUS_DELAYED, RealtimeEndpointRT
    from django.db.models import Q
    _, _, _, _completed_rt = _scheduler()

    completed_ids = _completed_rt()
    endpoints = list(
        RealtimeEndpointRT.objects.filter(
            hide_original=True,
            submission_id__in=completed_ids,
        ).filter(
            Q(fetch_status=FETCH_STATUS_ACTIVE)
            | Q(fetch_status=FETCH_STATUS_DELAYED, next_fetch_after__lte=timezone.now())
        ).values_list('id', 'interval')
    )

    scheduled = 0
    for index, (endpoint_id, interval) in enumerate(endpoints):
        interval = interval or 60
        # Rozloz start endpointow, aby uniknac jednoczesnego burstu.
        stagger = (index * RT_BOOTSTRAP_STAGGER_SECONDS) + random.randint(0, RT_BOOTSTRAP_JITTER_MAX_SECONDS)
        reserve_ttl = stagger + interval + RT_ALIVE_TTL_BUFFER_SECONDS
        # Atomowo rezerwuj slot: jesli marker juz istnieje, petla zyje -> pomijamy.
        if not cache.add(_rt_alive_key(endpoint_id), '1', timeout=reserve_ttl):
            continue
        fetch_realtime_endpoint_task.apply_async(
            args=[endpoint_id],
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
def fetch_realtime_endpoint_task(endpoint_id: int) -> dict:
    """
    Pobiera cache dla jednego RealtimeEndpointRT, a nastepnie planuje
    siebie na za `interval` sekund (self-scheduling loop).

    Bledy sieciowe sa zapisywane do FeedFetchError. Petla kontynuuje zgodnie
    z utrwalonym stanem feedu: stale interval po sukcesie, next_fetch_after po
    bledzie, stop dla pauzy automatycznej lub recznej.
    """
    from data_manager.models import (
        FETCH_STATUS_ACTIVE,
        FETCH_STATUS_AUTO_PAUSED,
        FETCH_STATUS_DELAYED,
        FETCH_STATUS_MANUAL_PAUSED,
        RealtimeEndpointRT,
    )
    _, _fetch, _, _completed_rt = _scheduler()

    lock_key = f'rt-endpoint-lock:{endpoint_id}'
    # cache.add zwroci False, jesli lock juz istnieje.
    lock_acquired = cache.add(lock_key, '1', timeout=RT_ENDPOINT_LOCK_TTL_SECONDS)
    if not lock_acquired:
        logger.info('RealtimeEndpointRT id=%d already running, skipping overlap', endpoint_id)
        return {'status': 'skipped_overlap', 'endpoint_id': endpoint_id}

    try:
        try:
            endpoint = RealtimeEndpointRT.objects.select_related(
                'submission'
            ).get(pk=endpoint_id)
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
        if endpoint.fetch_status in (FETCH_STATUS_AUTO_PAUSED, FETCH_STATUS_MANUAL_PAUSED):
            logger.info(
                'RealtimeEndpointRT id=%d fetch status=%s, stopping loop',
                endpoint_id,
                endpoint.fetch_status,
            )
            cache.delete(_rt_alive_key(endpoint_id))
            return {'status': endpoint.fetch_status, 'endpoint_id': endpoint_id}

        if endpoint.fetch_status == FETCH_STATUS_DELAYED and endpoint.next_fetch_after and endpoint.next_fetch_after > now:
            countdown = _seconds_until(endpoint.next_fetch_after)
            _mark_rt_alive(endpoint_id, countdown)
            fetch_realtime_endpoint_task.apply_async(args=[endpoint_id], countdown=countdown)
            return {
                'status': 'deferred',
                'endpoint_id': endpoint_id,
                'next_in': countdown,
                'next_fetch_after': endpoint.next_fetch_after,
            }

        try:
            result = _fetch(endpoint, now)
        except Exception as exc:
            logger.warning('fetch_realtime_endpoint_rt id=%d error: %s', endpoint_id, exc)
            result = 'error'

        endpoint.refresh_from_db(fields=[
            'fetch_status',
            'next_fetch_after',
            'fetch_failure_count',
            'interval',
        ])
        if endpoint.fetch_status == FETCH_STATUS_ACTIVE:
            countdown = endpoint.interval
        elif endpoint.fetch_status == FETCH_STATUS_DELAYED and endpoint.next_fetch_after:
            countdown = _seconds_until(endpoint.next_fetch_after)
        else:
            cache.delete(_rt_alive_key(endpoint_id))
            return {
                'status': result,
                'endpoint_id': endpoint_id,
                'fetch_status': endpoint.fetch_status,
                'fetch_failure_count': endpoint.fetch_failure_count,
            }

        _mark_rt_alive(endpoint_id, countdown)
        fetch_realtime_endpoint_task.apply_async(args=[endpoint_id], countdown=countdown)

        return {
            'status': result,
            'endpoint_id': endpoint_id,
            'fetch_status': endpoint.fetch_status,
            'fetch_failure_count': endpoint.fetch_failure_count,
            'next_in': countdown,
            'next_fetch_after': endpoint.next_fetch_after,
        }
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
    Validates a static GTFS feed using 'ghcr.io/mobilitydata/gtfs-validator'.

    Steps:
    1. Identify the input file (user upload 'file' OR server download 'cached_file').
    2. Prepare temporary output directory on HOST (accessible via bind mount).
    3. Run Docker container (DinD) with input/output mounts.
    4. Parse report.json.
    5. Update FeedValidationReport and Submission stage.
    """
    import json
    import shutil
    import logging
    import docker
    from django.conf import settings
    from data_manager.models import (
        StaticFeedEntry,
        FeedValidationReport,
        FeedSubmissionHistory
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

    def _set_validation_status(status: str, message: str | None = None) -> None:
        entry.validation_status = status
        entry.validation_message = message
        entry.save(update_fields=['validation_status', 'validation_message'])

    _set_validation_status(StaticFeedEntry.VALIDATION_PENDING, 'Validation in progress')

    def _reject_submission(reason: str, *, validation_error: bool = False) -> None:
        status = (
            StaticFeedEntry.VALIDATION_ERROR
            if validation_error
            else StaticFeedEntry.VALIDATION_INVALID
        )
        _set_validation_status(status, reason)
        submission = entry.submission
        if submission.current_stage >= 4:
            logger.warning(
                'GTFS validation failed for published submission %s; not reverting stage (was %s): %s',
                submission.id,
                submission.current_stage,
                reason,
            )
            return
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=FeedSubmissionHistory.EVENT_REJECTED,
            stage_before=submission.current_stage,
            stage_after=1,
            cause=reason,
            actor=None,
        )
        logger.warning(f"Submission {submission.id} rejected: {reason}")

    # 1. Determine which file to validate
    # Prefer cached_file if present (latest proxy download), else user file.
    # For non-proxy URL feeds, download once to a temp dir for validation only.
    validation_temp_dir = None
    file_field = entry.cached_file if entry.cached_file else entry.file

    if not file_field and entry.url:
        from urllib.parse import urlparse

        import requests
        from data_manager.net_security import OutboundURLBlocked, safe_get
        from data_manager.scheduler import _build_auth_headers

        validation_temp_dir = os.path.join(
            settings.MEDIA_ROOT,
            str(entry.submission_id),
            'validation',
            f'tmp_{entry.id}_{random.randint(1000, 9999)}',
        )
        os.makedirs(validation_temp_dir, exist_ok=True)
        try:
            response = safe_get(
                entry.url,
                headers=_build_auth_headers(entry.auth_type, entry.auth_value),
                timeout=60,
                max_bytes=settings.MAX_FEED_FILE_SIZE_BYTES,
            )
            response.raise_for_status()
            parsed = urlparse(entry.url)
            filename = os.path.basename(parsed.path) or 'feed.zip'
            container_file_path = os.path.join(validation_temp_dir, filename)
            with open(container_file_path, 'wb') as fh:
                fh.write(response.content)
            container_input_dir = validation_temp_dir
        except OutboundURLBlocked as exc:
            _reject_submission(str(exc), validation_error=True)
            shutil.rmtree(validation_temp_dir, ignore_errors=True)
            return
        except requests.exceptions.RequestException as exc:
            _reject_submission(
                f'Failed to download feed for validation: {exc}',
                validation_error=True,
            )
            shutil.rmtree(validation_temp_dir, ignore_errors=True)
            return
    elif not file_field:
        logger.info(f"No file found for StaticFeedEntry {entry_id}. Skipping validation.")
        _set_validation_status(
            StaticFeedEntry.VALIDATION_ERROR,
            'No file available for validation.',
        )
        return
    else:
        # file_field.name is relative to MEDIA_ROOT (e.g. '1/2/static/feed.zip')
        relative_path = file_field.name
        if relative_path.startswith('uploaded_data/'):
            relative_path = relative_path[len('uploaded_data/'):]
        filename = os.path.basename(relative_path)
        container_file_path = file_field.path
        container_input_dir = os.path.dirname(container_file_path)

    relative_dir = os.path.relpath(container_input_dir, settings.MEDIA_ROOT)
    # HOST_PROJECT_PATH is the absolute path to project root on the host machine
    host_project_path = os.environ.get('HOST_PROJECT_PATH', str(settings.BASE_DIR))
    host_media_root = os.environ.get('HOST_MEDIA_ROOT', os.path.join(host_project_path, 'uploaded_data'))

    # Host paths for Docker bind mounts
    host_input_dir = os.path.join(host_media_root, relative_dir)
    host_file_path = os.path.join(host_input_dir, filename)

    report_dir_name = f"validation_report_{entry.id}_{random.randint(1000, 9999)}"
    container_output_dir = os.path.join(container_input_dir, report_dir_name)
    host_output_dir = os.path.join(host_input_dir, report_dir_name)

    if not os.path.exists(container_file_path):
        _reject_submission(
            f"File not found in container: {container_file_path}",
            validation_error=True,
        )
        return

    # NOTE: Do not check host_file_path existence here.
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
        logger.error(f"Failed to create validation output dir: {e}")
        _set_validation_status(StaticFeedEntry.VALIDATION_ERROR, str(e))
        return

    logger.info(f"Validating {filename}...")
    logger.info(f"Host Input: {host_input_dir}, Host Output: {host_output_dir}")

    try:
        client = docker.from_env()
    except docker.errors.DockerException as e:
        _reject_submission(
            f"Cannot connect to Docker (check DOCKER_GID / docker.sock): {e}",
            validation_error=True,
        )
        return

    try:
        volumes = {
            host_input_dir: {'bind': '/input', 'mode': 'ro'},
            host_output_dir: {'bind': '/output', 'mode': 'rw'},
        }

        command = ["-i", f"/input/{filename}", "-o", "/output"]
        validator_uid = os.environ.get('UID', '1000')
        validator_gid = os.environ.get('GID', '1000')

        # Bound resource usage so a malicious/zip-bomb feed cannot exhaust the host.
        mem_limit = os.environ.get('GTFS_VALIDATOR_MEM_LIMIT', '2g')
        pids_limit = int(os.environ.get('GTFS_VALIDATOR_PIDS_LIMIT', '512'))
        run_kwargs = dict(
            image="ghcr.io/mobilitydata/gtfs-validator:latest",
            command=command,
            volumes=volumes,
            remove=True,
            detach=False,
            user=f"{validator_uid}:{validator_gid}",
            network_disabled=True,
            mem_limit=mem_limit,
            pids_limit=pids_limit,
            cap_drop=['ALL'],
            security_opt=['no-new-privileges:true'],
        )
        nano_cpus = os.environ.get('GTFS_VALIDATOR_NANO_CPUS')
        if nano_cpus:
            run_kwargs['nano_cpus'] = int(nano_cpus)

        container = client.containers.run(**run_kwargs)

        logger.info("Validation container finished.")

    except docker.errors.ContainerError as e:
        _reject_submission(f"Validator container failed: {e}", validation_error=True)
        return
    except Exception as e:
        _reject_submission(f"Docker execution error: {e}", validation_error=True)
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
        _reject_submission(
            f"report.json not found in output directory after {max_retries} retries: {container_output_dir}",
            validation_error=True,
        )
        return

    if not os.path.exists(report_file):
        _reject_submission(
            f"report.json not found in output directory: {container_output_dir}",
            validation_error=True,
        )
        return

    try:
        with open(report_file, 'r', encoding='utf-8') as f:
            report_data = json.load(f)
    except json.JSONDecodeError as e:
        _reject_submission(f"Failed to decode report.json: {e}", validation_error=True)
        return

    def extract_notice_counts(payload: dict) -> tuple[int, int, dict]:
        """Parse a GTFS Validator report payload and return (error_count, warning_count, error_code_counts).

        This mirrors the original nested function used inside validate_gtfs_feed_task.
        """
        summary = payload.get('summary', {}) if isinstance(payload, dict) else {}

        # Summary-based fallbacks
        error_fallback = (
            summary.get('errorCount') or summary.get('error_count') or summary.get('errors') or 0
        )
        warning_fallback = (
            summary.get('warningCount') or summary.get('warning_count') or summary.get('warnings') or 0
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

    # Cleanup output dir
    try:
        # remove output directory to avoid leaving random reports on disk
        shutil.rmtree(container_output_dir, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Failed to cleanup {container_output_dir}: {e}")
    if validation_temp_dir:
        shutil.rmtree(validation_temp_dir, ignore_errors=True)

    # Update Submission Stage
    submission = entry.submission

    if error_count > 0:
        # Build rejection_cause as "code: count" list
        if error_code_counts:
            parts = [f"{code}: {count}" for code, count in sorted(error_code_counts.items())]
            cause = ", ".join(parts)
        else:
            cause = f"Validation failed with {error_count} errors."
        _reject_submission(cause)
    else:
        _set_validation_status(StaticFeedEntry.VALIDATION_VALID, None)
        # Re-fetch submission fresh state in case it changed
        submission.refresh_from_db()
        current_stage = submission.current_stage
        # Prevent advancing if already advanced by something else
        if current_stage < 3:
            FeedSubmissionHistory.objects.create(
                submission=submission,
                event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
                stage_before=current_stage,
                stage_after=3,
                actor=None,
            )
            logger.info(f"Submission {submission.id} advanced to Stage 3 (valid GTFS, awaiting admin).")


@shared_task(name='data_manager.validate_realtime_submission')
def validate_realtime_submission_task(submission_id: int) -> dict:
    """
    Weryfikacja realtime (etap 2): link-check, opcjonalnie API walidatora GTFS-RT,
    oraz pierwsze pobranie cache dla endpointów z hide_original.

    Flow: etap 1 → 2 (start weryfikacji) → 3 po sukcesie; błąd → rejected, etap 1.
    """
    import json
    import os

    import requests
    from django.conf import settings

    from data_manager.models import (
        RealtimeSubmission,
        RealtimeSubmissionHistory,
        completed_submission_ids,
    )
    from data_manager.scheduler import _build_auth_headers, _fetch_realtime_endpoint_rt
    from data_manager.net_security import OutboundURLBlocked, assert_safe_outbound_url, safe_get

    max_link_bytes = getattr(settings, 'MAX_FEED_FILE_SIZE_BYTES', 200 * 1024 * 1024)

    try:
        rts = RealtimeSubmission.objects.select_related('static_submission').prefetch_related(
            'endpoints'
        ).get(pk=submission_id)
    except RealtimeSubmission.DoesNotExist:
        return {'status': 'not_found', 'id': submission_id}

    def _reject(reason: str) -> None:
        RealtimeSubmissionHistory.objects.create(
            submission=rts,
            event_type=RealtimeSubmissionHistory.EVENT_REJECTED,
            stage_before=rts.current_stage,
            stage_after=1,
            cause=reason,
            actor=None,
        )

    def _advance(stage_before: int, stage_after: int) -> None:
        RealtimeSubmissionHistory.objects.create(
            submission=rts,
            event_type=RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
            stage_before=stage_before,
            stage_after=stage_after,
            actor=None,
        )

    if rts.current_stage == 1:
        _advance(1, 2)

    if rts.protocol in (RealtimeSubmission.PROTOCOL_GTFS_RT, RealtimeSubmission.PROTOCOL_SIRI):
        if not rts.static_submission_id:
            _reject('Brak powiązania ze statycznym zgłoszeniem.')
            return {'status': 'rejected'}
        if rts.static_submission_id not in completed_submission_ids():
            _reject('Feed statyczny musi być opublikowany przed walidacją realtime.')
            return {'status': 'rejected'}

    endpoints = list(rts.endpoints.all())
    if not endpoints:
        _reject('Brak zdefiniowanych endpointów.')
        return {'status': 'rejected'}

    errors: list[str] = []

    if rts.protocol == RealtimeSubmission.PROTOCOL_GTFS_RT:
        base = getattr(settings, 'GTFS_RT_VALIDATOR_URL', os.environ.get(
            'GTFS_RT_VALIDATOR_URL', 'http://gtfs-realtime-validator:8080'
        ))
        static = rts.static_submission.static_entry
        static_url = None
        if static:
            if static.url and not static.hide_original:
                static_url = static.url
            elif static.url and static.cached_file:
                public = getattr(settings, 'PUBLIC_BASE_URL', '').rstrip('/')
                if public:
                    static_url = f"{public}/feed/{rts.static_submission_id}/{static.cached_file.name.split('/')[-1]}"
        for ep in endpoints:
            try:
                resp = safe_get(
                    ep.url,
                    headers=_build_auth_headers(ep.auth_type, ep.auth_value),
                    timeout=15,
                    max_bytes=max_link_bytes,
                )
                if resp.status_code >= 400:
                    errors.append(f'{ep.endpoint_type}: HTTP {resp.status_code}')
            except OutboundURLBlocked as exc:
                errors.append(f'{ep.endpoint_type}: blocked URL ({exc})')
            except Exception as exc:
                errors.append(f'{ep.endpoint_type}: {exc}')
        if static_url:
            try:
                assert_safe_outbound_url(static_url)
                r0 = requests.post(
                    f'{base.rstrip("/")}/api/gtfs',
                    json={'url': static_url},
                    timeout=60,
                )
                if r0.status_code >= 400:
                    errors.append(f'validator GTFS: HTTP {r0.status_code}')
                else:
                    gtfs_id = (r0.json() or {}).get('id') or (r0.json() or {}).get('gtfsFeedId')
                    for ep in endpoints:
                        r1 = requests.post(
                            f'{base.rstrip("/")}/api/gtfs-rt',
                            json={'url': ep.url, 'gtfsFeedId': gtfs_id},
                            timeout=60,
                        )
                        if r1.status_code >= 400:
                            errors.append(f'{ep.endpoint_type}: validator RT HTTP {r1.status_code}')
            except Exception as exc:
                errors.append(f'validator: {exc}')
    else:
        for ep in endpoints:
            try:
                resp = safe_get(
                    ep.url,
                    headers=_build_auth_headers(ep.auth_type, ep.auth_value),
                    timeout=15,
                    max_bytes=max_link_bytes,
                )
                if resp.status_code >= 400:
                    errors.append(f'{ep.endpoint_type}: HTTP {resp.status_code}')
                if rts.protocol == RealtimeSubmission.PROTOCOL_GBFS and ep.endpoint_type == 'gbfs':
                    try:
                        json.loads(resp.content.decode('utf-8', errors='strict'))
                    except Exception:
                        errors.append('gbfs: odpowiedź nie jest poprawnym JSON')
            except OutboundURLBlocked as exc:
                errors.append(f'{ep.endpoint_type}: blocked URL ({exc})')
            except Exception as exc:
                errors.append(f'{ep.endpoint_type}: {exc}')

    if not errors:
        now = timezone.now()
        for ep in endpoints:
            if not ep.hide_original:
                continue
            _fetch_realtime_endpoint_rt(ep, now)
            ep.refresh_from_db(fields=['cached_file', 'cached_at'])
            if not ep.cached_file:
                errors.append(f'{ep.endpoint_type}: nie udało się pobrać cache')

    if errors:
        _reject(' | '.join(errors))
        return {'status': 'rejected', 'errors': errors}

    cur = RealtimeSubmission.objects.get(pk=submission_id).current_stage
    if cur == 2:
        _advance(2, 3)
    elif cur == 1:
        _advance(1, 3)
    return {'status': 'ok', 'id': submission_id}
