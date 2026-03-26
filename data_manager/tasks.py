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


def _scheduler():
    """Leniwy import — unika circular imports przy starcie Celery."""
    from data_manager.scheduler import (
        _fetch_realtime_endpoint,
        _fetch_static_entry,
        _completed_submission_ids,
    )
    return _fetch_static_entry, _fetch_realtime_endpoint, _completed_submission_ids


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
    from data_manager.models import StaticFeedEntry
    from django.db.models import Q
    _, _, _completed = _scheduler()

    now_time = timezone.now().time().replace(second=0, microsecond=0)
    completed_ids = _completed()

    entry_ids = list(
        StaticFeedEntry.objects.filter(
            hide_original=True,
            url__isnull=False,
            submission_id__in=completed_ids,
        ).filter(
            Q(download_time_1=now_time) | Q(download_time_2=now_time)
        ).values_list('id', flat=True)
    )

    for entry_id in entry_ids:
        fetch_static_entry_task.delay(entry_id)

    logger.info('Dispatched %d static feed tasks for %s', len(entry_ids), now_time)
    return {'dispatched': len(entry_ids), 'time': str(now_time)}


@shared_task(
    name='data_manager.fetch_static_entry',
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,   # 60 s -> 120 s -> 240 s
    retry_jitter=True,
)
def fetch_static_entry_task(entry_id: int) -> dict:
    """Pobiera i zapisuje cache dla jednego StaticFeedEntry."""
    from data_manager.models import StaticFeedEntry
    _fetch, _, _ = _scheduler()

    try:
        entry = StaticFeedEntry.objects.get(pk=entry_id)
    except StaticFeedEntry.DoesNotExist:
        logger.warning('StaticFeedEntry id=%d not found, skipping', entry_id)
        return {'status': 'not_found', 'entry_id': entry_id}

    _fetch(entry)
    return {'status': 'ok', 'entry_id': entry_id}


# ---------------------------------------------------------------------------
# REALTIME — self-scheduling, bez pollingu
# ---------------------------------------------------------------------------

@shared_task(name='data_manager.bootstrap_realtime_tasks')
def bootstrap_realtime_tasks() -> dict:
    """
    Uruchamiany JEDNORAZOWO przez Beat przy starcie systemu.
    Inicjuje petle self-scheduling dla kazdego aktywnego endpointu RT.
    Po tym Beat nie jest juz potrzebny dla taskow RT.

    Bezpieczny do wielokrotnego wywolania: uruchomienia sa rozkladane
    w czasie, ale deduplikacja kolejek nie opiera sie na task_id.
    """
    from data_manager.models import RealtimeEndpoint
    _, _, _completed = _scheduler()

    completed_ids = _completed()
    endpoint_ids = list(
        RealtimeEndpoint.objects.filter(
            hide_original=True,
            entry__submission_id__in=completed_ids,
        ).values_list('id', flat=True)
    )

    for index, endpoint_id in enumerate(endpoint_ids):
        # Rozloz start endpointow, aby uniknac jednoczesnego burstu.
        stagger = (index * RT_BOOTSTRAP_STAGGER_SECONDS) + random.randint(0, RT_BOOTSTRAP_JITTER_MAX_SECONDS)
        fetch_realtime_endpoint_task.apply_async(
            args=[endpoint_id],
            countdown=stagger,
        )

    logger.info('Bootstrapped %d realtime endpoint tasks', len(endpoint_ids))
    return {'bootstrapped': len(endpoint_ids)}


@shared_task(
    name='data_manager.fetch_realtime_endpoint',
    max_retries=2,
    # Nie uzywamy autoretry — przy bledzie chcemy nadal zaplanowac kolejne
    # wykonanie (zeby endpoint nie "wypadl" z petli po 2 bledach z rzedu).
)
def fetch_realtime_endpoint_task(endpoint_id: int) -> dict:
    """
    Pobiera cache dla jednego RealtimeEndpoint, a nastepnie planuje
    siebie na za `interval` sekund (self-scheduling loop).

    Bledy sieciowe sa zapisywane do FeedFetchError — petla kontynuuje,
    ale rownolegle wykonania tego samego endpointu sa blokowane lockiem.
    """
    from data_manager.models import RealtimeEndpoint
    _, _fetch, _completed = _scheduler()

    lock_key = f'rt-endpoint-lock:{endpoint_id}'
    # cache.add zwroci False, jesli lock juz istnieje.
    lock_acquired = cache.add(lock_key, '1', timeout=RT_ENDPOINT_LOCK_TTL_SECONDS)
    if not lock_acquired:
        logger.info('RealtimeEndpoint id=%d already running, skipping overlap', endpoint_id)
        return {'status': 'skipped_overlap', 'endpoint_id': endpoint_id}

    try:
        try:
            endpoint = RealtimeEndpoint.objects.select_related(
                'entry__submission'
            ).get(pk=endpoint_id)
        except RealtimeEndpoint.DoesNotExist:
            logger.info('RealtimeEndpoint id=%d deleted, stopping loop', endpoint_id)
            return {'status': 'stopped', 'endpoint_id': endpoint_id}

        # Sprawdz czy submission wciaz jest aktywne (stage 4, nie cofniete).
        completed_ids = set(_completed())
        if endpoint.entry.submission_id not in completed_ids:
            logger.info(
                'RealtimeEndpoint id=%d submission no longer completed, stopping loop',
                endpoint_id,
            )
            return {'status': 'stopped', 'endpoint_id': endpoint_id}

        now = timezone.now()
        try:
            _fetch(endpoint, now)
            status = 'ok'
        except Exception as exc:
            # Blad jest zapisywany do FeedFetchError przez _fetch.
            logger.warning('fetch_realtime_endpoint id=%d error: %s', endpoint_id, exc)
            status = 'error'
        finally:
            # Planuj kolejne wykonanie niezaleznie od sukcesu/bledu.
            fetch_realtime_endpoint_task.apply_async(
                args=[endpoint_id],
                countdown=endpoint.interval,
            )

        return {'status': status, 'endpoint_id': endpoint_id, 'next_in': endpoint.interval}
    finally:
        cache.delete(lock_key)


# ---------------------------------------------------------------------------
# GTFS VALIDATION TASK
# ---------------------------------------------------------------------------

@shared_task(name='data_manager.validate_gtfs_feed', bind=True)
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
        entry = StaticFeedEntry.objects.get(id=entry_id)
    except StaticFeedEntry.DoesNotExist:
        logger.warning(f"StaticFeedEntry {entry_id} not found. Aborting validation.")
        return

    def _reject_submission(reason: str) -> None:
        submission = entry.submission
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
    # Prefer cached_file if present (latest download), else user file.
    file_field = entry.cached_file if entry.cached_file else entry.file
    if not file_field:
        logger.info(f"No file found for StaticFeedEntry {entry_id}. Skipping validation.")
        return

    # Calculate paths
    # file_field.name is relative to MEDIA_ROOT (e.g. '1/2/static/feed.zip')
    relative_path = file_field.name
    if relative_path.startswith('uploaded_data/'):
        # Backward-compat for existing files saved with the old prefix.
        relative_path = relative_path[len('uploaded_data/'):]
    relative_dir = os.path.dirname(relative_path)

    # HOST_PROJECT_PATH is the absolute path to project root on the host machine
    # Fetched from env var (injected by docker-compose) or default to BASE_DIR
    host_project_path = os.environ.get('HOST_PROJECT_PATH', str(settings.BASE_DIR))
    host_media_root = os.environ.get('HOST_MEDIA_ROOT', os.path.join(host_project_path, 'uploaded_data'))

    # Container paths (actual file on container FS)
    container_file_path = file_field.path
    container_input_dir = os.path.dirname(container_file_path)

    # Host paths for Docker bind mounts
    host_input_dir = os.path.join(host_media_root, relative_dir)
    filename = os.path.basename(relative_path)
    host_file_path = os.path.join(host_input_dir, filename)

    report_dir_name = f"validation_report_{entry.id}_{random.randint(1000,9999)}"
    container_output_dir = os.path.join(container_input_dir, report_dir_name)
    host_output_dir = os.path.join(host_input_dir, report_dir_name)

    if not os.path.exists(container_file_path):
        _reject_submission(f"File not found in container: {container_file_path}")
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
            os.chmod(container_output_dir, 0o777)
        except Exception:
            pass
    except OSError as e:
        logger.error(f"Failed to create validation output dir: {e}")
        return

    logger.info(f"Validating {filename}...")
    logger.info(f"Host Input: {host_input_dir}, Host Output: {host_output_dir}")

    # Run Docker
    client = docker.from_env()

    try:
        volumes = {
            host_input_dir: {'bind': '/input', 'mode': 'ro'},
            host_output_dir: {'bind': '/output', 'mode': 'rw'},
        }

        command = f"-i /input/{filename} -o /output"

        container = client.containers.run(
            image="ghcr.io/mobilitydata/gtfs-validator:latest",
            command=command,
            volumes=volumes,
            remove=True,
            detach=False,
            user=0,
        )

        logger.info("Validation container finished.")

    except docker.errors.ContainerError as e:
        _reject_submission(f"Validator container failed: {e}")
        return
    except Exception as e:
        _reject_submission(f"Docker execution error: {e}")
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
            f"report.json not found in output directory after {max_retries} retries: {container_output_dir}"
        )
        return

    if not os.path.exists(report_file):
        _reject_submission(f"report.json not found in output directory: {container_output_dir}")
        return

    try:
        with open(report_file, 'r', encoding='utf-8') as f:
            report_data = json.load(f)
    except json.JSONDecodeError as e:
        _reject_submission(f"Failed to decode report.json: {e}")
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
    if entry.validation_report_id:
        val_report = entry.validation_report
        val_report.report_json = report_data
        val_report.error_count = error_count
        val_report.warning_count = warning_count
        val_report.save()
    else:
        val_report = FeedValidationReport.objects.create(
            report_json=report_data,
            error_count=error_count,
            warning_count=warning_count,
        )
        # attach to entry
        entry.validation_report = val_report
        entry.save(update_fields=['validation_report'])

    # Cleanup output dir
    try:
        # remove output directory to avoid leaving random reports on disk
        shutil.rmtree(container_output_dir, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Failed to cleanup {container_output_dir}: {e}")

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
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
            stage_before=submission.current_stage,
            stage_after=3,
            actor=None,
        )
        logger.info(f"Submission {submission.id} advanced to Stage 3 (valid GTFS, awaiting admin).")
