"""POST /api/data_manager/realtime-submissions/ (docs/api/feeds.md).

DataProvider or Admin may create; the submission starts at stage 1 with an
`uploaded` (0 -> 1) history entry and realtime validation is queued
(docs/domain/feed-workflow.md). Celery publishing is recorded by the root
conftest's `celery_send_task` fixture; DNS is faked, so nothing leaves the
process.
"""

import pytest

from cases.models import TransportOrganization
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
)
from OtwarteDaneTransportowe.auth_roles import (
    ROLE_ADMIN,
    ROLE_BLOGGER,
    ROLE_DATA_PROVIDER,
    ROLE_EDITOR,
    ROLE_HELPER,
)

pytestmark = pytest.mark.django_db

URL = '/api/data_manager/realtime-submissions/'
VALIDATE_TASK = 'data_manager.validate_realtime_submission'
ANONYMOUS = None
PRIVATE_HOST = 'intranet.example.org'


@pytest.fixture(autouse=True)
def _private_host(fake_dns):
    """Endpoint URLs are SSRF-checked on input; PRIVATE_HOST resolves to a private IP."""
    fake_dns[PRIVATE_HOST] = '10.0.0.5'


@pytest.fixture
def org():
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def provider(make_user):
    return make_user(ROLE_DATA_PROVIDER, 'provider')


def _static_submission(org, user, *, data_type='gtfs', published=True):
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=user, data_type=data_type, name='Static'
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=(
            FeedSubmissionHistory.EVENT_COMPLETED
            if published
            else FeedSubmissionHistory.EVENT_UPLOADED
        ),
        stage_before=3 if published else 1,
        stage_after=4 if published else 2,
        actor=user,
    )
    return submission


def _gbfs_payload(org, **overrides):
    # static_submission: None is accepted as well as an omitted field, see
    # test_documented_gbfs_payload_without_static_submission_is_accepted.
    payload = {
        'transport_organization': org.id,
        'static_submission': None,
        'protocol': 'gbfs',
        'name': 'City bikes',
        'license': 'ODbL',
        'endpoints': [
            {
                'endpoint_type': 'gbfs',
                'url': 'https://example.org/gbfs.json',
                'is_original': True,
                'auth_type': '',
                'interval': 60,
            }
        ],
    }
    payload.update(overrides)
    return payload


def _gtfs_rt_payload(org, static_submission):
    return {
        'transport_organization': org.id,
        'static_submission': static_submission.id,
        'protocol': 'gtfs_rt',
        'name': 'Realtime',
        'license': 'CC BY 4.0',
        'endpoints': [
            {
                'endpoint_type': 'trip_update',
                'url': 'https://example.org/trip-updates.pb',
                'is_original': True,
                'auth_type': '',
                'interval': 30,
            },
            {
                'endpoint_type': 'vehicle_position',
                'url': 'https://example.org/vehicle-positions.pb',
                'is_original': True,
                'auth_type': 'api_key',
                'auth_value': 'secret',
                'interval': 15,
            },
        ],
    }


@pytest.fixture
def post_as(client_as, django_capture_on_commit_callbacks):
    """``post_as(user, payload)`` -> the response to creating a realtime submission."""

    def post(user, payload):
        # Validation is queued from transaction.on_commit, which never fires
        # inside the test's transaction unless captured.
        with django_capture_on_commit_callbacks(execute=True):
            return client_as(user).post(URL, payload, format='json')

    return post


def _validation_queued_for(celery_send_task):
    """Submission ids for which realtime validation was sent to Celery."""
    return [
        call.args[1][0] for call in celery_send_task.call_args_list if call.args[0] == VALIDATE_TASK
    ]


def test_data_provider_creates_gbfs_submission_at_stage_1(provider, org, post_as, celery_send_task):
    response = post_as(provider, _gbfs_payload(org))

    assert response.status_code == 201, response.data
    rts = RealtimeSubmission.objects.get(pk=response.data['id'])
    assert rts.submitted_by == provider
    assert response.data['submitted_by'] == provider.id
    assert response.data['protocol'] == 'gbfs'
    assert response.data['current_stage'] == 1
    assert response.data['current_stage_label'] == 'Step 1: Endpoints'
    assert response.data['is_rejected'] is False
    assert response.data['published_at'] is None
    assert [
        (h['event_type'], h['stage_before'], h['stage_after'], h['actor'])
        for h in response.data['history']
    ] == [('uploaded', 0, 1, 'provider')]
    assert [ep['endpoint_type'] for ep in response.data['endpoints']] == ['gbfs']
    assert _validation_queued_for(celery_send_task) == [rts.id]


def test_data_provider_creates_gtfs_rt_for_published_gtfs(provider, org, post_as, celery_send_task):
    static = _static_submission(org, provider)

    response = post_as(provider, _gtfs_rt_payload(org, static))

    assert response.status_code == 201, response.data
    assert response.data['static_submission'] == static.id
    assert response.data['current_stage'] == 1
    endpoints = {ep['endpoint_type']: ep for ep in response.data['endpoints']}
    assert set(endpoints) == {'trip_update', 'vehicle_position'}
    # A non-empty auth_type forces proxying; the secret is write-only.
    assert endpoints['vehicle_position']['hide_original'] is True
    assert endpoints['trip_update']['hide_original'] is False
    assert all('auth_value' not in ep for ep in response.data['endpoints'])
    stored = RealtimeEndpointRT.objects.get(endpoint_type='vehicle_position')
    assert stored.auth_value == 'secret'
    assert _validation_queued_for(celery_send_task) == [response.data['id']]


@pytest.mark.parametrize(
    'role, expected',
    [
        (ANONYMOUS, 401),
        (ROLE_HELPER, 403),
        (ROLE_BLOGGER, 403),
        (ROLE_EDITOR, 403),
        (ROLE_DATA_PROVIDER, 201),
        # The Admin group alone, without is_superuser.
        (ROLE_ADMIN, 201),
    ],
)
def test_create_by_role(role, expected, org, make_user, post_as, celery_send_task):
    user = make_user(role)

    response = post_as(user, _gbfs_payload(org))

    assert response.status_code == expected, response.data
    if expected == 201:
        assert response.data['submitted_by'] == user.id
        assert response.data['current_stage'] == 1
        assert [
            (h['event_type'], h['stage_before'], h['stage_after'], h['actor'])
            for h in response.data['history']
        ] == [('uploaded', 0, 1, user.username)]
        assert _validation_queued_for(celery_send_task) == [response.data['id']]
    else:
        assert not RealtimeSubmission.objects.exists()
        celery_send_task.assert_not_called()


def _no_endpoints(org, static, other_org):
    payload = _gbfs_payload(org)
    del payload['endpoints']
    return payload


def _empty_endpoints(org, static, other_org):
    return _gbfs_payload(org, endpoints=[])


def _gtfs_rt_without_static(org, static, other_org):
    payload = _gtfs_rt_payload(org, static)
    del payload['static_submission']
    return payload


def _gtfs_rt_for_unpublished_static(org, static, other_org):
    return _gtfs_rt_payload(org, _static_submission(org, static.submitted_by, published=False))


def _gtfs_rt_for_netex_static(org, static, other_org):
    return _gtfs_rt_payload(org, _static_submission(org, static.submitted_by, data_type='netex'))


def _static_of_another_organization(org, static, other_org):
    return {**_gtfs_rt_payload(org, static), 'transport_organization': other_org.id}


def _gbfs_with_static(org, static, other_org):
    return _gbfs_payload(org, static_submission=static.id)


def _endpoint_type_of_another_protocol(org, static, other_org):
    endpoint = {**_gbfs_payload(org)['endpoints'][0], 'endpoint_type': 'trip_update'}
    return _gbfs_payload(org, endpoints=[endpoint])


def _duplicate_endpoint_type(org, static, other_org):
    endpoint = _gbfs_payload(org)['endpoints'][0]
    return _gbfs_payload(org, endpoints=[endpoint, dict(endpoint)])


def _private_endpoint_url(org, static, other_org):
    endpoint = {**_gbfs_payload(org)['endpoints'][0], 'url': f'https://{PRIVATE_HOST}/gbfs.json'}
    return _gbfs_payload(org, endpoints=[endpoint])


@pytest.mark.parametrize(
    'make_payload, error_field',
    [
        (_no_endpoints, 'endpoints'),
        (_empty_endpoints, 'endpoints'),
        (_gtfs_rt_without_static, 'static_submission'),
        (_gtfs_rt_for_unpublished_static, 'static_submission'),
        (_gtfs_rt_for_netex_static, 'static_submission'),
        (_static_of_another_organization, 'static_submission'),
        (_gbfs_with_static, 'static_submission'),
        (_endpoint_type_of_another_protocol, 'endpoints'),
        (_duplicate_endpoint_type, 'endpoints'),
        (_private_endpoint_url, 'endpoints'),
    ],
)
def test_invalid_payload_is_rejected(
    make_payload, error_field, provider, org, post_as, celery_send_task
):
    static = _static_submission(org, provider)
    other_org = TransportOrganization.objects.create(region='R2', transport_organization='Other')

    response = post_as(provider, make_payload(org, static, other_org))

    assert response.status_code == 400, response.data
    assert error_field in response.data, response.data
    assert not RealtimeSubmission.objects.exists()
    celery_send_task.assert_not_called()


def test_second_realtime_of_same_protocol_for_one_static_is_rejected(
    provider, org, post_as, celery_send_task
):
    static = _static_submission(org, provider)
    first = post_as(provider, _gtfs_rt_payload(org, static))
    assert first.status_code == 201, first.data

    response = post_as(provider, _gtfs_rt_payload(org, static))

    assert response.status_code == 400, response.data
    assert response.data['non_field_errors'][0].code == 'unique'
    assert RealtimeSubmission.objects.count() == 1
    assert _validation_queued_for(celery_send_task) == [first.data['id']]


def test_documented_gbfs_payload_without_static_submission_is_accepted(provider, org, post_as):
    payload = _gbfs_payload(org)
    del payload['static_submission']

    response = post_as(provider, payload)

    assert response.status_code == 201, response.data
