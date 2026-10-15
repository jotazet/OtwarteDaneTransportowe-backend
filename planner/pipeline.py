"""Graph build pipeline, run only from Celery tasks (planner/tasks.py).

A build collects the newest file of every enabled planner feed, stops the
serving OTP (the planner's maintenance window), test-builds each changed feed
on its own, builds the street graph when the map or the feed area changed,
builds the full graph and switches the ``serving`` symlink to it. Any failure
leaves the previous graph serving. See ADR 0009 and docs/domain/journey-planner.md.
"""

import hashlib
import json
import logging
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from data_manager.models import completed_submission_ids
from data_manager.net_security import OutboundURLBlocked, ResponseTooLarge, safe_download
from planner import osm, otp_config, otp_runtime, search_index
from planner.models import GraphBuild, OsmDownload, PlannerFeed, PlannerRentalSource

logger = logging.getLogger(__name__)

# A running build refreshes its heartbeat at least once a minute.
HEARTBEAT_STALE_AFTER = timedelta(minutes=15)
SERVER_START_TIMEOUT_SECONDS = 20 * 60
SERVER_POLL_SECONDS = 10
# OTP's exit code for data/config problems (OtpAppException), e.g. no trips in
# the service period. 3 is the JVM's -XX:+ExitOnOutOfMemoryError exit code.
EXIT_DATA_ERROR = 100
EXIT_OUT_OF_MEMORY = 3
LOG_EXCERPT_CHARS = 4000
STREET_OSM_FILE = 'street.osm.pbf'
DOWNLOAD_TIMEOUT_SECONDS = 60
FETCH_RETRY_DELAY_SECONDS = 5


class BuildFailed(Exception):
    def __init__(self, code: str, log: str = ''):
        super().__init__(code)
        self.code = code
        self.log = log


class FeedUnavailable(Exception):
    pass


@dataclass
class Candidate:
    feed: PlannerFeed
    outcome: str
    path: str | None = None
    sha256: str = ''
    version_at: str | None = None
    error_code: str = ''
    log_excerpt: str = ''
    realtime_skipped: list = field(default_factory=list)
    # OTP updater types polled for the feed, e.g. 'vehicle-positions'.
    realtime_polled: list = field(default_factory=list)

    def result(self) -> dict:
        return {
            'planner_feed': self.feed.id,
            'feed_id': self.feed.feed_id,
            'submission': self.feed.submission_id,
            'outcome': self.outcome,
            'sha256': self.sha256,
            'version_at': self.version_at,
            'error_code': self.error_code,
            'log_excerpt': self.log_excerpt,
            'realtime_skipped': self.realtime_skipped,
            'realtime_polled': self.realtime_polled,
        }


def data_path(*parts) -> str:
    return os.path.join(settings.PLANNER_DATA_DIR, *parts)


def host_path(path: str) -> str:
    """The same location as the host's Docker daemon sees it."""
    return os.path.join(
        settings.HOST_PLANNER_DATA_DIR, os.path.relpath(path, settings.PLANNER_DATA_DIR)
    )


def snapshot_path(feed: PlannerFeed) -> str:
    return data_path('snapshots', f'{feed.feed_id}.zip')


def graph_dir(build: GraphBuild) -> str:
    return data_path('graphs', str(build.rollback_of_id or build.id))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_build(build_id: int) -> None:
    with transaction.atomic():
        build = GraphBuild.objects.select_for_update().filter(pk=build_id).first()
        if build is None or build.status != GraphBuild.STATUS_QUEUED:
            return
        now = timezone.now()
        build.status = GraphBuild.STATUS_RUNNING
        build.started_at = build.heartbeat_at = now
        build.save(update_fields=['status', 'started_at', 'heartbeat_at'])

    workdir = data_path('builds', str(build.id))
    try:
        if build.rollback_of_id:
            _stop_serving(build)
            included = [r['feed_id'] for r in build.feed_results if r['outcome'] != 'excluded']
            _activate(build, included, fresh=[])
        else:
            _build_and_activate(build, workdir)
    except BuildFailed as exc:
        _finish_failed(build, exc.code, exc.log)
    except otp_runtime.DockerUnavailable as exc:
        _finish_failed(build, 'planner.docker_unavailable', str(exc))
    except Exception:  # noqa: BLE001 - any unexpected failure must still end the build
        logger.exception('Graph build %s crashed', build.id)
        _finish_failed(build, 'planner.build_failed')
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if build.otp_stopped_at:
            _start_serving()
    refresh_index()


# Poland (~2.1 GB) takes 1-2 minutes from Geofabrik on a server link.
OSM_DOWNLOAD_TIMEOUT_SECONDS = 3600
OSM_READ_TIMEOUT_SECONDS = 60


def download_osm(download_id: int) -> None:
    """Replace the OSM extract with the file at the download's URL, if it is a valid PBF.

    Never runs during a build (services._queue_job). The file is written next to
    the current one and swapped in atomically only after osmium reads its header;
    on any failure the current file stays.
    """
    with transaction.atomic():
        download = OsmDownload.objects.select_for_update().filter(pk=download_id).first()
        if download is None or download.status != OsmDownload.STATUS_QUEUED:
            return
        download.status = OsmDownload.STATUS_RUNNING
        download.started_at = timezone.now()
        download.save(update_fields=['status', 'started_at'])

    target = data_path(settings.PLANNER_OSM_FILE)
    partial = f'{target}.download'
    os.makedirs(os.path.dirname(target), exist_ok=True)
    error_code = ''
    try:
        with (
            open(partial, 'w+b') as file,
            safe_download(
                download.url,
                timeout=OSM_READ_TIMEOUT_SECONDS,
                total_timeout=OSM_DOWNLOAD_TIMEOUT_SECONDS,
                max_bytes=settings.PLANNER_OSM_MAX_BYTES,
                into=file,
            ) as (response, _),
        ):
            response.raise_for_status()
        download.data_timestamp = osm.pbf_header(partial)['timestamp']
        download.size_bytes = os.path.getsize(partial)
        os.replace(partial, target)
    except ResponseTooLarge:
        error_code = 'planner.osm_too_large'
    except OutboundURLBlocked:
        error_code = 'planner.osm_url_blocked'
    except osm.OsmPreparationError:
        error_code = 'planner.osm_invalid'
    except Exception:  # noqa: BLE001 - any failure must still end the download, like a build
        logger.warning('OSM download %s failed', download.id, exc_info=True)
        error_code = 'planner.osm_download_failed'
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    download.status = OsmDownload.STATUS_FAILED if error_code else OsmDownload.STATUS_SUCCEEDED
    download.error_code = error_code
    download.finished_at = timezone.now()
    download.save(
        update_fields=['status', 'error_code', 'size_bytes', 'data_timestamp', 'finished_at']
    )


def refresh_index() -> None:
    """Best effort: the watchdog retries while the active graph has no index."""
    try:
        search_index.refresh_active()
    except Exception:  # noqa: BLE001 - search keeps the previous index; never fail a build over it
        logger.warning('Could not refresh the planner search index', exc_info=True)


def ensure_serving() -> None:
    """Bring the active graph back up unless a build is using the maintenance window.

    Covers a worker killed mid-build (deploy, reboot), after which nothing else
    would restart the stopped OTP container.
    """
    if GraphBuild.objects.filter(status=GraphBuild.STATUS_RUNNING).exists():
        return
    otp_runtime.remove_build_containers()
    active = GraphBuild.objects.filter(is_active=True).first()
    if active is None:
        return
    _point_serving_at(graph_dir(active))
    if not otp_runtime.server_running():
        otp_runtime.start_server()


def fail_stale_osm_downloads() -> int:
    """Mark downloads whose worker disappeared as failed; returns how many."""
    now = timezone.now()
    return OsmDownload.objects.filter(
        Q(status=OsmDownload.STATUS_QUEUED, created_at__lt=now - HEARTBEAT_STALE_AFTER)
        | Q(
            status=OsmDownload.STATUS_RUNNING,
            started_at__lt=now
            - timedelta(seconds=OSM_DOWNLOAD_TIMEOUT_SECONDS)
            - HEARTBEAT_STALE_AFTER,
        )
    ).update(
        status=OsmDownload.STATUS_FAILED,
        error_code='planner.osm_download_interrupted',
        finished_at=now,
    )


def fail_stale_builds() -> int:
    """Mark builds whose worker disappeared as failed; returns how many."""
    cutoff = timezone.now() - HEARTBEAT_STALE_AFTER
    stale = GraphBuild.objects.filter(
        status=GraphBuild.STATUS_RUNNING, heartbeat_at__lt=cutoff
    ) | GraphBuild.objects.filter(status=GraphBuild.STATUS_QUEUED, created_at__lt=cutoff)
    stale = list(stale)
    for build in stale:
        _finish_failed(build, 'planner.build_interrupted')
    return len(stale)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _build_and_activate(build: GraphBuild, workdir: str) -> None:
    os.makedirs(os.path.join(workdir, 'feeds'), exist_ok=True)
    candidates = _collect_candidates(build, workdir)
    _record_results(build, candidates)
    if not any(c.path for c in candidates):
        raise BuildFailed('planner.no_feeds')

    _stop_serving(build)
    _test_changed_feeds(build, candidates, workdir)
    _record_results(build, candidates)
    included = [c for c in candidates if c.path]
    if not included:
        raise BuildFailed('planner.no_feeds')

    street_graph = _ensure_street_graph(build, candidates)
    _build_graph(build, included, workdir, street_graph)

    fresh = [c for c in included if c.outcome == GraphBuild.OUTCOME_FRESH]
    updaters, skipped = otp_config.realtime_updaters(
        [c.feed for c in fresh if c.feed.include_realtime]
    )
    for candidate in fresh:
        feed_id = candidate.feed.feed_id
        candidate.realtime_skipped = [s for s in skipped if s['feed_id'] == feed_id]
        candidate.realtime_polled = sorted({u['type'] for u in updaters if u['feedId'] == feed_id})
    _record_results(build, candidates)
    rental, build.rental_results = otp_config.rental_updaters(
        PlannerRentalSource.objects.filter(is_enabled=True).prefetch_related(
            'submission__endpoints'
        )
    )
    build.save(update_fields=['rental_results'])

    target = graph_dir(build)
    os.makedirs(target, exist_ok=True)
    os.replace(os.path.join(workdir, 'graph.obj'), os.path.join(target, 'graph.obj'))
    _write_json(os.path.join(target, 'otp-config.json'), otp_config.otp_config())
    _write_json(
        os.path.join(target, 'router-config.json'), otp_config.router_config(updaters + rental)
    )
    _activate(build, [c.feed.feed_id for c in included], fresh=fresh)


def _collect_candidates(build: GraphBuild, workdir: str) -> list[Candidate]:
    published = set(completed_submission_ids())
    candidates = []
    feeds = PlannerFeed.objects.filter(is_enabled=True).select_related('submission__static_entry')
    for feed in feeds:
        # Downloads of sources ODT does not proxy can take minutes each.
        _heartbeat(build)
        if feed.submission_id not in published:
            candidates.append(
                Candidate(
                    feed, GraphBuild.OUTCOME_EXCLUDED, error_code='planner.feed_not_published'
                )
            )
            continue
        target = os.path.join(workdir, 'feeds', f'{feed.feed_id}.zip')
        try:
            version_at = _fetch_feed(feed, target)
        except FeedUnavailable:
            candidates.append(_fall_back(feed, target, 'planner.feed_source_unavailable'))
            continue
        candidates.append(
            Candidate(
                feed,
                GraphBuild.OUTCOME_FRESH,
                path=target,
                sha256=_sha256(target),
                version_at=version_at.isoformat(),
            )
        )
    return candidates


def _fetch_feed(feed: PlannerFeed, target: str):
    """Copy (or download) the feed's current file to ``target``; returns its version time.

    A proxied or uploaded file may be mid-rewrite (OverwriteStorage deletes and
    rewrites in place), so a copy that is not a valid ZIP is retried once.
    """
    entry = getattr(feed.submission, 'static_entry', None)
    if entry is None:
        raise FeedUnavailable
    for attempt in range(2):
        if attempt:
            # The file may have been replaced (and renamed) by a fetch meanwhile.
            _sleep(FETCH_RETRY_DELAY_SECONDS)
            entry.refresh_from_db()
        try:
            source = entry.validation_source_file
            if source:
                with source.open('rb') as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                version_at = entry.cached_at or entry.uploaded_at
            elif entry.url:
                with safe_download(
                    entry.url,
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                    max_bytes=settings.MAX_FEED_FILE_SIZE_BYTES,
                    total_timeout=settings.FEED_FETCH_TOTAL_TIMEOUT_SECONDS,
                ) as (response, body):
                    response.raise_for_status()
                    with open(target, 'wb') as dst:
                        shutil.copyfileobj(body, dst)
                version_at = timezone.now()
            else:
                raise FeedUnavailable
        except (OSError, OutboundURLBlocked, requests.exceptions.RequestException):
            continue
        if _is_valid_zip(target):
            return version_at
    raise FeedUnavailable


def _fall_back(feed: PlannerFeed, target: str, error_code: str, log: str = '') -> Candidate:
    """Apply the feed's failure policy: its last good version, or nothing."""
    snapshot = snapshot_path(feed)
    log = log[-LOG_EXCERPT_CHARS:]
    # last_good_sha256 ties the snapshot to this feed: a feed deleted and created
    # again with the same feed_id must not inherit the old file.
    if (
        feed.failure_policy == PlannerFeed.POLICY_KEEP_LAST_GOOD
        and feed.last_good_sha256
        and os.path.exists(snapshot)
    ):
        shutil.copyfile(snapshot, target)
        return Candidate(
            feed,
            GraphBuild.OUTCOME_STALE,
            path=target,
            sha256=feed.last_good_sha256,
            version_at=feed.last_good_at.isoformat() if feed.last_good_at else None,
            error_code=error_code,
            log_excerpt=log,
        )
    return _excluded(feed, target, error_code, log)


def _excluded(feed: PlannerFeed, path: str, error_code: str, log: str = '') -> Candidate:
    if os.path.exists(path):
        os.remove(path)
    return Candidate(
        feed,
        GraphBuild.OUTCOME_EXCLUDED,
        error_code=error_code,
        log_excerpt=log[-LOG_EXCERPT_CHARS:],
    )


def _test_changed_feeds(build: GraphBuild, candidates: list[Candidate], workdir: str) -> None:
    """Build every new feed version on its own; OTP has no per-feed error isolation."""
    previous = GraphBuild.objects.filter(is_active=True).first()
    image_changed = previous is None or previous.otp_image != build.otp_image
    for index, candidate in enumerate(candidates):
        if candidate.outcome == GraphBuild.OUTCOME_EXCLUDED:
            continue
        # Unchanged files passed before (a last good version has that sha256
        # too); a new OTP version re-tests everything, last good versions included.
        if not image_changed and candidate.sha256 == candidate.feed.last_good_sha256:
            continue
        testdir = os.path.join(workdir, 'tests', candidate.feed.feed_id)
        os.makedirs(testdir, exist_ok=True)
        _write_json(
            os.path.join(testdir, 'build-config.json'),
            otp_config.build_config({candidate.feed.feed_id: 'feed.zip'}),
        )
        result = otp_runtime.run_otp(
            host_path(testdir),
            ['--build', '--save'],
            heap=settings.PLANNER_OTP_FEED_TEST_HEAP,
            mem_limit=settings.PLANNER_OTP_FEED_TEST_MEM_LIMIT,
            mounts={host_path(candidate.path): f'{otp_runtime.OTP_HOME}/feed.zip'},
            on_tick=lambda: _heartbeat(build),
        )
        shutil.rmtree(testdir, ignore_errors=True)
        _heartbeat(build)
        if result.exit_code == 0:
            continue
        if result.exit_code == EXIT_DATA_ERROR and 'no trips' in result.log_tail.lower():
            code = 'planner.feed_no_service_in_period'
        else:
            code = 'planner.feed_invalid'
        if candidate.outcome == GraphBuild.OUTCOME_STALE:
            candidates[index] = _excluded(candidate.feed, candidate.path, code, result.log_tail)
        else:
            candidates[index] = _fall_back(candidate.feed, candidate.path, code, result.log_tail)


def _ensure_street_graph(build: GraphBuild, candidates: list[Candidate]) -> str:
    osm_file = data_path(settings.PLANNER_OSM_FILE)
    if not os.path.exists(osm_file):
        raise BuildFailed('planner.osm_missing')
    # Excluded feeds keep their area (through their last good version), so one
    # failing feed does not force a street graph rebuild now and again later.
    area_files = [c.path for c in candidates if c.path]
    area_files += [
        snapshot_path(c.feed)
        for c in candidates
        if not c.path and c.feed.last_good_sha256 and os.path.exists(snapshot_path(c.feed))
    ]
    cells = osm.stop_cells(area_files)
    street_dir = data_path('street', osm.street_graph_key(cells, osm_file, build.otp_image))
    street_graph = os.path.join(street_dir, 'streetGraph.obj')
    if os.path.exists(street_graph):
        return street_graph

    tmp_dir = f'{street_dir}.tmp'
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)
    try:
        osm.prepare_osm(osm_file, cells, os.path.join(tmp_dir, STREET_OSM_FILE))
        _write_json(
            os.path.join(tmp_dir, 'build-config.json'),
            otp_config.build_config({}, osm_file=STREET_OSM_FILE),
        )
        result = _run_build_container(build, tmp_dir, ['--buildStreet'])
        if result.exit_code != 0 or not os.path.exists(os.path.join(tmp_dir, 'streetGraph.obj')):
            raise BuildFailed(
                _build_error_code(result, 'planner.street_build_failed'), result.log_tail
            )
        os.remove(os.path.join(tmp_dir, STREET_OSM_FILE))
    except osm.OsmPreparationError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise BuildFailed('planner.osm_preparation_failed', str(exc)) from exc
    except BuildFailed:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    # One street graph is kept: older ones belong to maps or areas no longer used.
    for other in os.listdir(data_path('street')):
        if other != os.path.basename(tmp_dir):
            shutil.rmtree(data_path('street', other), ignore_errors=True)
    os.replace(tmp_dir, street_dir)
    return street_graph


def _run_build_container(build, directory: str, args: list[str], mounts=None):
    return otp_runtime.run_otp(
        host_path(directory),
        args,
        heap=settings.PLANNER_OTP_BUILD_HEAP,
        mem_limit=settings.PLANNER_OTP_BUILD_MEM_LIMIT,
        memswap_limit=settings.PLANNER_OTP_BUILD_MEMSWAP_LIMIT,
        mounts=mounts,
        on_tick=lambda: _heartbeat(build),
    )


def _build_graph(build, included: list[Candidate], workdir: str, street_graph: str) -> None:
    feed_files = {c.feed.feed_id: f'feeds/{c.feed.feed_id}.zip' for c in included}
    _write_json(os.path.join(workdir, 'build-config.json'), otp_config.build_config(feed_files))
    result = _run_build_container(
        build,
        workdir,
        ['--loadStreet', '--save'],
        mounts={host_path(street_graph): f'{otp_runtime.OTP_HOME}/streetGraph.obj'},
    )
    if result.exit_code != 0 or not os.path.exists(os.path.join(workdir, 'graph.obj')):
        raise BuildFailed(_build_error_code(result, 'planner.graph_build_failed'), result.log_tail)


def _activate(build: GraphBuild, included_feed_ids: list[str], fresh: list[Candidate]) -> None:
    """Serve the build's graph; on any problem put the previous graph back."""
    target = graph_dir(build)
    if not os.path.exists(os.path.join(target, 'graph.obj')):
        raise BuildFailed('planner.graph_missing')
    serving = data_path('serving')
    previous_target = os.readlink(serving) if os.path.islink(serving) else None
    _point_serving_at(target)
    otp_runtime.start_server()
    if not _wait_until_healthy(build) or not _serves_feeds(included_feed_ids):
        otp_runtime.stop_server()
        if previous_target:
            _point_serving_at(data_path(previous_target))
        else:
            # First build: nothing to go back to, so serve nothing rather than this graph.
            os.remove(serving)
        raise BuildFailed('planner.activation_failed')

    # Copies first (may fail), then the database, then atomic renames, so a
    # snapshot never disagrees with the last_good_sha256 that describes it.
    staged = [_stage_snapshot(candidate) for candidate in fresh]
    staged = [paths for paths in staged if paths]
    now = timezone.now()
    with transaction.atomic():
        GraphBuild.objects.filter(is_active=True).update(is_active=False)
        build.is_active = True
        build.finished_at = now
        build.status = GraphBuild.STATUS_SUCCEEDED
        build.save(update_fields=['is_active', 'finished_at', 'status'])
        for candidate in fresh:
            PlannerFeed.objects.filter(pk=candidate.feed.pk).update(
                last_good_sha256=candidate.sha256, last_good_at=now
            )
    for tmp, snapshot in staged:
        os.replace(tmp, snapshot)
    _remove_unused_graphs(current=target)
    logger.info('Graph build %s is now serving', build.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stop_serving(build: GraphBuild) -> None:
    build.otp_stopped_at = timezone.now()
    build.save(update_fields=['otp_stopped_at'])
    try:
        otp_runtime.stop_server()
    except Exception as exc:  # noqa: BLE001 - docker errors vary; the build must not proceed
        raise BuildFailed('planner.docker_unavailable', str(exc)) from exc


def _start_serving() -> None:
    try:
        if not otp_runtime.server_running():
            otp_runtime.start_server()
    except Exception:  # noqa: BLE001 - the watchdog retries every few minutes
        logger.exception('Could not start the serving OTP container')


def _point_serving_at(target: str) -> None:
    """Atomically repoint the ``serving`` symlink (Docker resolves it on start)."""
    serving = data_path('serving')
    if os.path.isdir(serving) and not os.path.islink(serving):
        # Docker created an empty directory before the first build.
        os.rmdir(serving)
    tmp = f'{serving}.tmp'
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.relpath(target, settings.PLANNER_DATA_DIR), tmp)
    os.replace(tmp, serving)


def _wait_until_healthy(build: GraphBuild) -> bool:
    deadline = timezone.now() + timedelta(seconds=SERVER_START_TIMEOUT_SECONDS)
    while not otp_runtime.is_healthy():
        if timezone.now() >= deadline:
            return False
        _heartbeat(build)
        _sleep(SERVER_POLL_SECONDS)
    return True


def _sleep(seconds: int) -> None:
    time.sleep(seconds)


def _serves_feeds(feed_ids: list[str]) -> bool:
    try:
        data = otp_runtime.query('{ feeds { feedId } }')
    except otp_runtime.OtpUnavailable:
        return False
    return set(feed_ids) <= {feed['feedId'] for feed in data.get('feeds') or []}


def _stage_snapshot(candidate: Candidate) -> tuple[str, str] | None:
    """Link the new last good version next to its snapshot; None if it is unchanged."""
    snapshot = snapshot_path(candidate.feed)
    if candidate.sha256 == candidate.feed.last_good_sha256 and os.path.exists(snapshot):
        return None
    os.makedirs(os.path.dirname(snapshot), exist_ok=True)
    tmp = f'{snapshot}.tmp'
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.link(candidate.path, tmp)
    return tmp, snapshot


def _remove_unused_graphs(current: str) -> None:
    """Keep the serving graph and the newest other one (for a rollback)."""
    keep = {current}
    previous = (
        GraphBuild.objects.filter(status=GraphBuild.STATUS_SUCCEEDED, rollback_of__isnull=True)
        .exclude(is_active=True)
        .first()
    )
    if previous:
        keep.add(graph_dir(previous))
    root = data_path('graphs')
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if path not in keep:
            shutil.rmtree(path, ignore_errors=True)


def _record_results(build: GraphBuild, candidates: list[Candidate]) -> None:
    build.feed_results = [c.result() for c in candidates]
    build.heartbeat_at = timezone.now()
    build.save(update_fields=['feed_results', 'heartbeat_at'])


def _heartbeat(build: GraphBuild) -> None:
    GraphBuild.objects.filter(pk=build.pk).update(heartbeat_at=timezone.now())


def _finish_failed(build: GraphBuild, code: str, log: str = '') -> None:
    build.status = GraphBuild.STATUS_FAILED
    build.finished_at = timezone.now()
    build.error_code = code
    build.log_excerpt = log[-LOG_EXCERPT_CHARS:]
    build.save(update_fields=['status', 'finished_at', 'error_code', 'log_excerpt'])
    logger.error('Graph build %s failed: %s', build.id, code)


def _build_error_code(result: otp_runtime.RunResult, default: str) -> str:
    if result.oom or result.exit_code == EXIT_OUT_OF_MEMORY:
        return 'planner.build_out_of_memory'
    return default


def _is_valid_zip(path: str) -> bool:
    """A copy cut off mid-rewrite has no central directory; OTP's test catches the rest."""
    try:
        with zipfile.ZipFile(path):
            return True
    except (OSError, zipfile.BadZipFile):
        return False


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: str, payload: dict) -> None:
    with open(path, 'w') as fh:
        json.dump(payload, fh, indent=1)
