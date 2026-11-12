"""Celery stores no task results and web requests reuse database connections (issue #75)."""

from celery.backends.base import DisabledBackend
from django.conf import settings

from accounts.tasks import flush_expired_tokens_task
from data_manager.tasks import fetch_realtime_endpoint_task
from OtwarteDaneTransportowe.celery import app


def test_celery_has_no_result_backend():
    assert app.conf.task_ignore_result is True
    assert app.conf.result_backend is None
    assert isinstance(app.backend, DisabledBackend)


def test_tasks_do_not_store_results():
    # The realtime poll runs every few seconds per endpoint; it used to leave a result key each time.
    assert fetch_realtime_endpoint_task.ignore_result is True
    assert flush_expired_tokens_task.ignore_result is True


def test_database_connections_are_reused_with_health_checks():
    database = settings.DATABASES['default']
    assert database['CONN_MAX_AGE'] == 60
    assert database['CONN_HEALTH_CHECKS'] is True


def test_expired_jwts_are_flushed_daily():
    entry = settings.CELERY_BEAT_SCHEDULE['flush-expired-tokens']
    assert entry['task'] == flush_expired_tokens_task.name
