"""Celery tasks of the journey planner. Builds and Docker work run on the 'planner' queue."""

from celery import shared_task
from celery.utils.log import get_task_logger

from data_manager.tasks import enqueue
from planner import pipeline, services
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


@shared_task(name='planner.scheduled_graph_build')
def scheduled_graph_build_task():
    try:
        services.request_build(GraphBuild.TRIGGER_SCHEDULE)
    except services.BuildInProgress:
        logger.info('Scheduled graph build skipped: a build is already queued or running')


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
    if GraphBuild.objects.filter(is_active=True).exists():
        enqueue(ensure_serving_task)


@shared_task(name='planner.ensure_serving', soft_time_limit=300, time_limit=360)
def ensure_serving_task():
    pipeline.ensure_serving()
