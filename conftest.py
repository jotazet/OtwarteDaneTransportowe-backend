"""Global pytest fixtures.

Throttling and the shared cache are production concerns; during tests we disable
DRF throttling and clear the cache between tests so rate limits never cause
flaky failures. Celery tasks are recorded instead of being sent to a broker.
`make_user` and `client_as` build role users and API clients for permission tests.
"""

import itertools
from unittest.mock import patch

import pytest
from celery.app.base import Celery
from django.contrib.auth.models import Group
from rest_framework.test import APIClient


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


@pytest.fixture
def make_user(django_user_model):
    """Factory: ``make_user(role, username=None)`` -> a fresh user in the ``role`` group.

    A role of None stands for an anonymous visitor and gives None, so
    ``client_as(make_user(role))`` covers the anonymous row of a role table too.
    Users get no password, so create_user() stores an unusable one instead of
    running the password hasher (~0.1 s a user); tests log in with
    force_authenticate().
    """
    numbers = itertools.count(1)

    def make(role, username=None):
        if role is None:
            return None
        user = django_user_model.objects.create_user(username or f'user-{next(numbers)}')
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def client_as():
    """Factory: ``client_as(user)`` -> an APIClient logged in as ``user``; None is anonymous."""

    def make(user):
        client = APIClient()
        # force_authenticate(None) would log out, which saves a throwaway session.
        if user is not None:
            client.force_authenticate(user=user)
        return client

    return make
