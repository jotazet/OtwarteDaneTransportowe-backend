"""Realtime verification that runs out of time stays at stage 2 and is retried."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.utils import timezone

from cases.models import TransportOrganization
from data_manager import rejection_reasons as rr
from data_manager.models import RealtimeSubmissionHistory
from data_manager.tasks import (
    RT_VERIFICATION_STUCK_AFTER_SECONDS,
    retry_stuck_realtime_verifications,
    validate_realtime_submission_task,
)
from data_manager.tests.test_realtime_rejection_reasons import (
    assert_rejected_with,
    make_rts,
    response,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def provider(make_user):
    return make_user('DataProvider')


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='Test Region', transport_organization='Org')


def add_entry(rts, stage_after, *, event_type=None, actor=None, age_seconds=0):
    entry = RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=event_type or RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=rts.current_stage,
        stage_after=stage_after,
        actor=actor,
    )
    # created_at is auto_now_add: backdate it with an update, and keep the
    # earlier entries older so this one stays the latest.
    created_at = timezone.now() - timedelta(seconds=age_seconds)
    rts.history.exclude(pk=entry.pk).filter(created_at__gte=created_at).update(
        created_at=created_at - timedelta(seconds=1)
    )
    RealtimeSubmissionHistory.objects.filter(pk=entry.pk).update(created_at=created_at)


def queued_verifications(send_task):
    name = validate_realtime_submission_task.name
    return [call.args[1][0] for call in send_task.call_args_list if call.args[0] == name]


def test_downloads_share_one_budget(org, provider):
    rts = make_rts(
        org, provider, 'gbfs', endpoint_types=['gbfs', 'system_information'], hide_original=True
    )

    with (
        patch('data_manager.tasks.RT_VERIFICATION_BUDGET_SECONDS', 30),
        patch('data_manager.net_security.safe_get', return_value=response(200, b'{}')) as get,
        patch('data_manager.scheduler._fetch_realtime_endpoint_rt') as fetch,
    ):
        validate_realtime_submission_task(rts.id)

    # Each download gets what is left of the budget, never the full per-download cap.
    timeouts = [call.kwargs['total_timeout'] for call in get.call_args_list + fetch.call_args_list]
    assert len(timeouts) == 4
    assert all(0 < timeout <= 30 for timeout in timeouts)
    assert settings.REALTIME_FETCH_TOTAL_TIMEOUT_SECONDS > 30


def test_endpoints_past_the_budget_are_rejected_as_timeouts(org, provider):
    rts = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs', 'system_information'])

    with (
        patch('data_manager.tasks.RT_VERIFICATION_BUDGET_SECONDS', 0),
        patch('data_manager.net_security.safe_get') as get,
    ):
        validate_realtime_submission_task(rts.id)

    get.assert_not_called()
    assert_rejected_with(
        rts,
        [
            {'code': rr.ENDPOINT_TIMEOUT, 'details': {'endpoint_type': endpoint_type}}
            for endpoint_type in ('gbfs', 'system_information')
        ],
    )


def test_soft_time_limit_in_cache_fetch_records_no_endpoint_failure(org, provider):
    rts = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'], hide_original=True)

    with (
        patch('data_manager.net_security.safe_get', return_value=response(200, b'{}')),
        patch('data_manager.scheduler.safe_download', side_effect=SoftTimeLimitExceeded()),
    ):
        result = validate_realtime_submission_task(rts.id)

    assert result == {'status': 'timed_out', 'id': rts.id}
    endpoint = rts.endpoints.get()
    assert endpoint.fetch_failure_count == 0
    assert not endpoint.fetch_errors.exists()
    rts.refresh_from_db()
    assert rts.current_stage == 2
    assert not rts.is_rejected


def test_soft_time_limit_keeps_stage_2_instead_of_rejecting(org, provider):
    rts = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs', 'system_information'])

    with patch('data_manager.net_security.safe_get', side_effect=SoftTimeLimitExceeded()) as get:
        result = validate_realtime_submission_task(rts.id)

    assert result == {'status': 'timed_out', 'id': rts.id}
    # The exception left the endpoint loop: it was not recorded as the first
    # endpoint's endpoint_fetch_failed with the loop going on to the next one.
    assert get.call_count == 1
    rts.refresh_from_db()
    assert rts.current_stage == 2
    assert not rts.is_rejected


def test_sweep_requeues_only_lost_verifications(org, provider, celery_send_task):
    old = RT_VERIFICATION_STUCK_AFTER_SECONDS + 60
    stuck = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'])
    add_entry(stuck, 2, age_seconds=old)
    recent = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'])
    add_entry(recent, 2, age_seconds=60)
    set_by_reviewer = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'])
    add_entry(set_by_reviewer, 2, actor=provider, age_seconds=old)
    rejected = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'])
    add_entry(rejected, 2, age_seconds=old * 2)
    add_entry(rejected, 1, event_type=RealtimeSubmissionHistory.EVENT_REJECTED, age_seconds=old)
    awaiting_review = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'])
    add_entry(awaiting_review, 3, age_seconds=old)

    assert retry_stuck_realtime_verifications() == {'stuck': 1, 'queued': 1}
    assert queued_verifications(celery_send_task) == [stuck.id]

    # The stage-2 timestamp does not move on a retry: the next sweep must not
    # queue a second run while the first may still be in progress.
    assert retry_stuck_realtime_verifications() == {'stuck': 1, 'queued': 0}
    assert queued_verifications(celery_send_task) == [stuck.id]


def test_sweep_retries_on_next_run_when_broker_is_down(org, provider, celery_send_task):
    stuck = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'])
    add_entry(stuck, 2, age_seconds=RT_VERIFICATION_STUCK_AFTER_SECONDS + 60)

    with patch('data_manager.tasks.enqueue', return_value=False):
        assert retry_stuck_realtime_verifications() == {'stuck': 1, 'queued': 0}

    assert retry_stuck_realtime_verifications() == {'stuck': 1, 'queued': 1}
    assert queued_verifications(celery_send_task) == [stuck.id]
