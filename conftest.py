"""Global pytest fixtures.

Throttling and the shared cache are production concerns; during tests we disable
DRF throttling and clear the cache between tests so rate limits never cause
flaky failures. Celery tasks are recorded instead of being sent to a broker.
"""

from unittest.mock import patch

import pytest
from celery.app.base import Celery


@pytest.fixture(autouse=True)
def celery_send_task():
    """Record queued Celery tasks instead of publishing them to a broker.

    Without this, every enqueue() in a test connected to the default
    redis://localhost:6379 broker: with no Redis running it spent ~19 s on
    reconnect retries before enqueue() swallowed the error, and with the dev
    stack's Redis up it queued real jobs for the dev worker.

    Celery.send_task is the one place where a task leaves the process, so
    patching it is enough: a test that patches a task's .delay/.apply_async
    still gets its own mock, and CELERY_TASK_ALWAYS_EAGER still runs tasks
    inline because the eager branch of apply_async() never calls send_task.
    Request this fixture to assert on what was queued; each call is
    ``send_task(task_name, args, kwargs, **options)``.
    """
    with patch.object(Celery, 'send_task') as send_task:
        yield send_task


@pytest.fixture(autouse=True)
def _disable_throttling(settings):
    rest = dict(getattr(settings, 'REST_FRAMEWORK', {}))
    rest['DEFAULT_THROTTLE_CLASSES'] = []
    rest['DEFAULT_THROTTLE_RATES'] = {}
    # Reassigning REST_FRAMEWORK fires setting_changed so DRF reloads api_settings.
    settings.REST_FRAMEWORK = rest
    yield


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
