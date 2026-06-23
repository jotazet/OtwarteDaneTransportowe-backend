"""Regression tests for the realtime self-scheduling loop.

Guards against the previous behaviour where the periodic ``bootstrap_realtime_tasks``
re-queued every active endpoint on each run, accumulating duplicate fetch chains.
"""
import pytest

from cases.models import TransportOrganization
from data_manager import tasks
from data_manager.models import (
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def published_gbfs_endpoint(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    org = TransportOrganization.objects.create(
        region='R', transport_organization='Org'
    )
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
    )
    endpoint = RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.org/gbfs.json',
        hide_original=True,
        interval=30,
    )
    return endpoint


def test_bootstrap_does_not_duplicate_live_chains(published_gbfs_endpoint, monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks.fetch_realtime_endpoint_task,
        'apply_async',
        lambda *args, **kwargs: calls.append(kwargs),
    )

    first = tasks.bootstrap_realtime_tasks()
    second = tasks.bootstrap_realtime_tasks()

    # First run seeds the endpoint; second run sees a live alive-marker and skips.
    assert first['bootstrapped'] == 1
    assert second['bootstrapped'] == 0
    assert second['skipped_alive'] == 1
    assert len(calls) == 1


def test_bootstrap_reseeds_after_marker_expires(published_gbfs_endpoint, monkeypatch):
    from django.core.cache import cache

    calls = []
    monkeypatch.setattr(
        tasks.fetch_realtime_endpoint_task,
        'apply_async',
        lambda *args, **kwargs: calls.append(kwargs),
    )

    tasks.bootstrap_realtime_tasks()
    # Simulate a dead chain: its alive-marker expired / was cleared.
    cache.delete(tasks._rt_alive_key(published_gbfs_endpoint.id))

    again = tasks.bootstrap_realtime_tasks()
    assert again['bootstrapped'] == 1
    assert len(calls) == 2
