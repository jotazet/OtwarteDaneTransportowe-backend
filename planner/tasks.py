"""Celery tasks of the journey planner. Builds and Docker work run on the 'planner' queue."""

from celery import shared_task
from celery.utils.log import get_task_logger

from data_manager.tasks import enqueue
from planner import pipeline, request_logs, services
from planner.models import GraphBuild

logger = get_task_logger(__name__)

# All of Poland took ~13 min to build (+8 min when the street graph is rebuilt)
# on the spike machine with swap in RAM; disk swap on the VPS is slower, so
# allow hours (docs/deployment.md, sizing).
BUILD_SOFT_TIME_LIMIT = 6 * 3600


@shared_task(
    name='planner.run_graph_build',
    soft_time_limit=BUILD_SOFT_TIME_LIMIT,
    time_limit=BUILD_SOFT_TIME_LIMIT + 600,
)
def run_graph_build_task(build_id: int):
    pipeline.run_build(build_id)


# A scheduled build that meets an OSM download (a few minutes) waits for it.
SCHEDULED_BUILD_RETRY_SECONDS = 600
SCHEDULED_BUILD_RETRIES = 6


@shared_task(name='planner.scheduled_graph_build', bind=True, max_retries=SCHEDULED_BUILD_RETRIES)
def scheduled_graph_build_task(self):
    try:
        services.request_build(GraphBuild.TRIGGER_SCHEDULE)
    except services.BuildInProgress:
        logger.info('Scheduled graph build skipped: a build is already queued or running')
    except services.OsmDownloadInProgress as exc:
        logger.warning('Scheduled graph build waits for an OSM download')
        raise self.retry(exc=exc, countdown=SCHEDULED_BUILD_RETRY_SECONDS) from None


@shared_task(name='planner.watchdog')
def watchdog_task():
    """Fail builds whose worker died and make sure the active graph is served.

    Runs on 'default' (DB only); the Docker part goes to the planner worker, and
    only once there is a graph to serve, so a stack without the planner never
    queues anything.
    """
    failed = pipeline.fail_stale_builds()
    if failed:
        logger.error('Marked %d interrupted graph build(s) as failed', failed)
    failed = pipeline.fail_stale_osm_downloads()
    if failed:
        logger.error('Marked %d interrupted OSM download(s) as failed', failed)
    active = GraphBuild.objects.filter(is_active=True).first()
    if active is not None:
        enqueue(ensure_serving_task)
        if active.indexed_at is None:
            enqueue(refresh_index_task)


@shared_task(name='planner.ensure_serving', soft_time_limit=300, time_limit=360)
def ensure_serving_task():
    pipeline.ensure_serving()


@shared_task(name='planner.refresh_index', soft_time_limit=600, time_limit=660)
def refresh_index_task():
    pipeline.refresh_index()


@shared_task(
    name='planner.download_osm',
    soft_time_limit=pipeline.OSM_DOWNLOAD_TIMEOUT_SECONDS + 300,
    time_limit=pipeline.OSM_DOWNLOAD_TIMEOUT_SECONDS + 600,
)
def download_osm_task(download_id: int):
    pipeline.download_osm(download_id)


@shared_task(name='planner.maintain_request_logs', soft_time_limit=1800, time_limit=1900)
def maintain_request_logs_task():
    request_logs.roll_up_and_purge()
