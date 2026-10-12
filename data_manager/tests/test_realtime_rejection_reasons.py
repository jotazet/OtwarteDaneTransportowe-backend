"""Automatic realtime rejections are stored and served as codes (ADR 0006)."""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager import rejection_reasons as rr
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)
from data_manager.net_security import OutboundURLBlocked, ResponseTooLarge
from data_manager.tasks import validate_realtime_submission_task
from data_manager.tests.test_flow import gtfs_rt_feed_bytes

pytestmark = pytest.mark.django_db

legacy_migration = importlib.import_module(
    'data_manager.migrations.0011_realtime_rejection_codes_from_text'
)


@pytest.fixture
def provider():
    user = get_user_model().objects.create_user('provider', 'p@example.com', 'password')
    user.groups.add(Group.objects.get_or_create(name='DataProvider')[0])
    return user


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='Test Region', transport_organization='Org')


@pytest.fixture
def published_static(org, provider):
    static = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=provider, data_type='gtfs'
    )
    FeedSubmissionHistory.objects.create(
        submission=static,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
    )
    return static


def make_rts(org, provider, protocol, static=None, endpoint_types=(), hide_original=False):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=provider,
        protocol=protocol,
        static_submission=static,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
        actor=provider,
    )
    for endpoint_type in endpoint_types:
        RealtimeEndpointRT.objects.create(
            submission=rts,
            endpoint_type=endpoint_type,
            url=f'https://example.org/rt/{endpoint_type}',
            hide_original=hide_original,
        )
    return rts


def response(status_code=200, content=b''):
    return SimpleNamespace(status_code=status_code, content=content)


def assert_rejected_with(rts, reasons):
    rts.refresh_from_db()
    assert rts.is_rejected
    assert rts.current_stage == 1
    assert rts.rejection_reasons == reasons
    # No prose for automatic rejections: the frontend owns the wording.
    assert rts.rejection_cause is None


# ---------------------------------------------------------------------------
# One test per code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('protocol', 'static_data_type', 'endpoint_types', 'code'),
    [
        ('gtfs_rt', None, ['trip_update'], rr.STATIC_SUBMISSION_MISSING),
        ('siri', 'netex', ['et'], rr.STATIC_SUBMISSION_NOT_PUBLISHED),
        ('gbfs', None, [], rr.NO_ENDPOINTS),
    ],
)
def test_submission_level_problem_is_rejected_with_code(
    org, provider, protocol, static_data_type, endpoint_types, code
):
    static = (
        FeedSubmission.objects.create(
            transport_organization=org, submitted_by=provider, data_type=static_data_type
        )
        if static_data_type
        else None
    )
    rts = make_rts(org, provider, protocol, static=static, endpoint_types=endpoint_types)

    validate_realtime_submission_task(rts.id)

    assert_rejected_with(rts, [{'code': code, 'details': {}}])


@pytest.mark.parametrize(
    ('safe_get_kwargs', 'code', 'extra_details'),
    [
        ({'return_value': response(404)}, rr.ENDPOINT_HTTP_ERROR, {'status': 404}),
        ({'return_value': response(200, b'')}, rr.ENDPOINT_CONTENT_EMPTY, {}),
        (
            {'return_value': response(200, b'\x00\x01 not protobuf')},
            rr.ENDPOINT_CONTENT_INVALID,
            {'protocol': 'gtfs_rt'},
        ),
        (
            {'side_effect': OutboundURLBlocked('URL resolves to a non-public IP address.')},
            rr.ENDPOINT_URL_BLOCKED,
            {},
        ),
        (
            {'side_effect': ResponseTooLarge(1024)},
            rr.ENDPOINT_TOO_LARGE,
            {'limit_bytes': 1024},
        ),
        ({'side_effect': requests.exceptions.ReadTimeout('slow')}, rr.ENDPOINT_TIMEOUT, {}),
        # ConnectTimeout is both a Timeout and a ConnectionError: it counts as a timeout.
        ({'side_effect': requests.exceptions.ConnectTimeout('slow')}, rr.ENDPOINT_TIMEOUT, {}),
        (
            {'side_effect': requests.exceptions.ConnectionError('refused')},
            rr.ENDPOINT_UNREACHABLE,
            {},
        ),
        ({'side_effect': RuntimeError('boom')}, rr.ENDPOINT_FETCH_FAILED, {}),
    ],
)
def test_endpoint_failure_is_rejected_with_code(
    org, provider, published_static, safe_get_kwargs, code, extra_details
):
    rts = make_rts(
        org, provider, 'gtfs_rt', static=published_static, endpoint_types=['trip_update']
    )

    with patch('data_manager.net_security.safe_get', **safe_get_kwargs):
        result = validate_realtime_submission_task(rts.id)

    expected = [{'code': code, 'details': {'endpoint_type': 'trip_update', **extra_details}}]
    assert result == {'status': 'rejected', 'reasons': expected}
    assert_rejected_with(rts, expected)


def test_failed_first_cache_fetch_is_rejected_with_code(org, provider):
    rts = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'], hide_original=True)

    with (
        patch('data_manager.net_security.safe_get', return_value=response(200, b'{}')),
        # The first fetch stores nothing, as when the source fails mid-verification.
        patch('data_manager.scheduler._fetch_realtime_endpoint_rt'),
    ):
        validate_realtime_submission_task(rts.id)

    assert_rejected_with(
        rts, [{'code': rr.ENDPOINT_CACHE_FAILED, 'details': {'endpoint_type': 'gbfs'}}]
    )


def test_every_failing_endpoint_gets_its_own_reason(org, provider, published_static):
    rts = make_rts(
        org,
        provider,
        'gtfs_rt',
        static=published_static,
        endpoint_types=['trip_update', 'vehicle_position', 'service_alert'],
    )
    responses = {
        'https://example.org/rt/trip_update': response(500),
        'https://example.org/rt/vehicle_position': response(200, gtfs_rt_feed_bytes()),
        'https://example.org/rt/service_alert': response(200, b''),
    }

    with patch('data_manager.net_security.safe_get', side_effect=lambda url, **kw: responses[url]):
        validate_realtime_submission_task(rts.id)

    rts.refresh_from_db()
    assert sorted(rts.rejection_reasons, key=lambda r: r['details']['endpoint_type']) == [
        {'code': rr.ENDPOINT_CONTENT_EMPTY, 'details': {'endpoint_type': 'service_alert'}},
        {
            'code': rr.ENDPOINT_HTTP_ERROR,
            'details': {'endpoint_type': 'trip_update', 'status': 500},
        },
    ]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_serves_reasons_on_detail_history_and_list(org, provider):
    rts = make_rts(org, provider, 'gbfs')
    validate_realtime_submission_task(rts.id)
    client = APIClient()
    client.force_authenticate(user=provider)

    detail = client.get(f'/api/data_manager/realtime-submissions/{rts.id}/').data
    submissions = client.get('/api/data_manager/submissions/').data

    expected = [{'code': rr.NO_ENDPOINTS, 'details': {}}]
    assert detail['is_rejected'] is True
    assert detail['rejection_reasons'] == expected
    assert detail['rejection_cause'] is None
    assert detail['history'][0]['reasons'] == expected
    assert detail['history'][0]['cause'] is None
    (listed,) = [r for r in submissions['realtime'] if r['id'] == rts.id]
    assert listed['has_rejection_cause'] is True


def test_reviewer_rejection_keeps_free_text_and_has_no_reasons(org, provider):
    admin = get_user_model().objects.create_superuser('admin', 'a@example.com', 'password')
    rts = make_rts(org, provider, 'gbfs', endpoint_types=['gbfs'])
    client = APIClient()
    client.force_authenticate(user=admin)

    patched = client.patch(
        f'/api/data_manager/realtime-submissions/{rts.id}/',
        {'rejection_cause': 'Wrong licence.'},
        format='json',
    )

    assert patched.status_code == 200, patched.data
    detail = client.get(f'/api/data_manager/realtime-submissions/{rts.id}/').data
    assert detail['rejection_cause'] == 'Wrong licence.'
    assert detail['rejection_reasons'] is None


# ---------------------------------------------------------------------------
# Legacy texts (migration 0011)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('cause', 'protocol', 'expected'),
    [
        (
            'Brak powiązania ze statycznym zgłoszeniem.',
            'gtfs_rt',
            [{'code': rr.STATIC_SUBMISSION_MISSING, 'details': {}}],
        ),
        (
            'Feed statyczny musi być opublikowany przed walidacją realtime.',
            'siri',
            [{'code': rr.STATIC_SUBMISSION_NOT_PUBLISHED, 'details': {}}],
        ),
        ('Brak zdefiniowanych endpointów.', 'gbfs', [{'code': rr.NO_ENDPOINTS, 'details': {}}]),
        (
            'trip_update: HTTP 404 | vehicle_position: nie udało się pobrać cache',
            'gtfs_rt',
            [
                {
                    'code': rr.ENDPOINT_HTTP_ERROR,
                    'details': {'endpoint_type': 'trip_update', 'status': 404},
                },
                {
                    'code': rr.ENDPOINT_CACHE_FAILED,
                    'details': {'endpoint_type': 'vehicle_position'},
                },
            ],
        ),
        (
            'et: Fetched content is empty.',
            'siri',
            [{'code': rr.ENDPOINT_CONTENT_EMPTY, 'details': {'endpoint_type': 'et'}}],
        ),
        (
            'trip_update: Fetched content is not a valid GTFS-RT protobuf feed (Error parsing).',
            'gtfs_rt',
            [
                {
                    'code': rr.ENDPOINT_CONTENT_INVALID,
                    'details': {'endpoint_type': 'trip_update', 'protocol': 'gtfs_rt'},
                }
            ],
        ),
        (
            'gbfs: odpowiedź nie jest poprawnym JSON',
            'gbfs',
            [
                {
                    'code': rr.ENDPOINT_CONTENT_INVALID,
                    'details': {'endpoint_type': 'gbfs', 'protocol': 'gbfs'},
                }
            ],
        ),
        (
            'sm: blocked URL (URL resolves to a non-public IP address.)',
            'siri',
            [{'code': rr.ENDPOINT_URL_BLOCKED, 'details': {'endpoint_type': 'sm'}}],
        ),
        (
            'gbfs: blocked URL (Response body exceeds the 33554432 byte limit.)',
            'gbfs',
            [
                {
                    'code': rr.ENDPOINT_TOO_LARGE,
                    'details': {'endpoint_type': 'gbfs', 'limit_bytes': 33554432},
                }
            ],
        ),
        (
            'gbfs: Total download time budget exceeded.',
            'gbfs',
            [{'code': rr.ENDPOINT_TIMEOUT, 'details': {'endpoint_type': 'gbfs'}}],
        ),
    ],
)
def test_legacy_text_is_converted_to_reasons(cause, protocol, expected):
    assert legacy_migration.reasons_from_text(cause, protocol) == expected


@pytest.mark.parametrize(
    'cause',
    [
        'Wrong licence.',  # reviewer text
        'trip_update: validator RT HTTP 500',  # removed RT validator
        'validator: Connection refused',
        # One unknown part keeps the whole text: no half-converted rows.
        'trip_update: HTTP 404 | vehicle_position: HTTPSConnectionPool(host=...)',
    ],
)
def test_unrecognised_legacy_text_is_left_as_text(cause):
    assert legacy_migration.reasons_from_text(cause, 'gtfs_rt') is None


def test_legacy_migration_converts_rows_and_reverses(org, provider):
    rts = make_rts(org, provider, 'gbfs')
    legacy = RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_REJECTED,
        stage_before=2,
        stage_after=1,
        cause='Brak zdefiniowanych endpointów.',
    )
    reviewer = RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_REJECTED,
        stage_before=3,
        stage_after=1,
        cause='Wrong licence.',
    )

    legacy_migration.forwards(apps, None)
    legacy.refresh_from_db()
    reviewer.refresh_from_db()
    assert legacy.reasons == [{'code': rr.NO_ENDPOINTS, 'details': {}}]
    assert legacy.cause == 'Brak zdefiniowanych endpointów.'
    assert reviewer.reasons is None

    legacy_migration.backwards(apps, None)
    legacy.refresh_from_db()
    assert legacy.reasons is None
    assert legacy.cause == 'Brak zdefiniowanych endpointów.'
