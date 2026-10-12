"""Planner operations shared by the admin API and Celery tasks (no DRF here)."""

import json
import os

from django.conf import settings
from django.db import connection, transaction
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from data_manager.tasks import enqueue_on_commit
from planner.models import GraphBuild

SCHEDULE_TASK_NAME = 'planner-graph-build'
DEFAULT_SCHEDULE = {'enabled': False, 'weekdays': [3, 7], 'time_utc': '01:40'}
# Any constant shared by every build request: serialises them in Postgres.
BUILD_REQUEST_LOCK = 7_331_001


class BuildInProgress(Exception):
    pass


class BuildNotActivatable(Exception):
    pass


def request_build(trigger: str, user=None) -> GraphBuild:
    """Queue a graph build unless one is already queued or running."""
    return _queue_build(trigger=trigger, requested_by=user, otp_image=settings.PLANNER_OTP_IMAGE)


def request_rollback(target: GraphBuild, user=None) -> GraphBuild:
    """Serve an earlier build's graph again, if it is still on disk."""
    from planner.pipeline import graph_dir

    if (
        target.status != GraphBuild.STATUS_SUCCEEDED
        or target.is_active
        or target.otp_image != settings.PLANNER_OTP_IMAGE
        or not os.path.exists(os.path.join(graph_dir(target), 'graph.obj'))
    ):
        raise BuildNotActivatable
    return _queue_build(
        trigger=GraphBuild.TRIGGER_ROLLBACK,
        requested_by=user,
        rollback_of=target.rollback_of or target,
        otp_image=target.otp_image,
        street_graph_key=target.street_graph_key,
        feed_results=target.feed_results,
    )


def _queue_build(**fields) -> GraphBuild:
    from planner.tasks import run_graph_build_task

    with transaction.atomic():
        # Serialises concurrent requests: a row lock cannot cover a row that does not exist yet.
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [BUILD_REQUEST_LOCK])
        if GraphBuild.objects.filter(status__in=GraphBuild.ACTIVE_STATUSES).exists():
            raise BuildInProgress
        build = GraphBuild.objects.create(**fields)
        enqueue_on_commit(run_graph_build_task, build.id)
    return build


def maintenance_active() -> bool:
    """True while a build has the serving OTP stopped."""
    return GraphBuild.objects.filter(
        status=GraphBuild.STATUS_RUNNING, otp_stopped_at__isnull=False
    ).exists()


# ---------------------------------------------------------------------------
# Build schedule (django-celery-beat, edited at runtime)
# ---------------------------------------------------------------------------

# ISO weekday (1 = Monday ... 7 = Sunday) -> cron day_of_week (0 = Sunday).
_ISO_TO_CRON = {1: '1', 2: '2', 3: '3', 4: '4', 5: '5', 6: '6', 7: '0'}
_CRON_TO_ISO = {cron: iso for iso, cron in _ISO_TO_CRON.items()}


def get_build_schedule() -> dict:
    task = PeriodicTask.objects.select_related('crontab').filter(name=SCHEDULE_TASK_NAME).first()
    if task is None or task.crontab is None:
        return dict(DEFAULT_SCHEDULE)
    crontab = task.crontab
    try:
        weekdays = sorted(_CRON_TO_ISO[day] for day in crontab.day_of_week.split(','))
        time_utc = f'{int(crontab.hour):02d}:{int(crontab.minute):02d}'
    except (KeyError, ValueError):
        # Edited by hand (e.g. in the Django admin) into a form the panel does
        # not use; show the default so it can be saved again.
        return {**DEFAULT_SCHEDULE, 'enabled': task.enabled}
    return {'enabled': task.enabled, 'weekdays': weekdays, 'time_utc': time_utc}


def set_build_schedule(*, enabled: bool, weekdays: list[int], time_utc: str) -> dict:
    hour, minute = (int(part) for part in time_utc.split(':'))
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute=str(minute),
        hour=str(hour),
        day_of_week=','.join(sorted(_ISO_TO_CRON[day] for day in set(weekdays))),
        day_of_month='*',
        month_of_year='*',
        timezone='UTC',
    )
    PeriodicTask.objects.update_or_create(
        name=SCHEDULE_TASK_NAME,
        defaults={
            'task': 'planner.scheduled_graph_build',
            'crontab': crontab,
            'interval': None,
            'enabled': enabled,
            # Without this, beat treats a run time that passed since the last
            # run as due and starts a build the moment the schedule is saved.
            'last_run_at': None,
            'kwargs': json.dumps({}),
        },
    )
    return get_build_schedule()
