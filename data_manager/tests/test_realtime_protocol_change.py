"""Changing a realtime submission's protocol or static feed without `endpoints`.

The stored endpoints must still fit the new protocol, and the owner's change
restarts verification like an endpoint change does (docs/api/feeds.md).
"""

import pytest

from cases.models import TransportOrganization
from data_manager.models import RealtimeSubmission, RealtimeSubmissionHistory
from data_manager.tests.test_realtime_create import (
    URL,
    _gbfs_payload,
    _gtfs_rt_payload,
    _static_submission,
    _validation_queued_for,
)
from OtwarteDaneTransportowe.auth_roles import ROLE_DATA_PROVIDER

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('fake_dns')]


@pytest.fixture
def org():
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def owner(make_user):
    return make_user(ROLE_DATA_PROVIDER, 'owner')


@pytest.fixture
def send(client_as, django_capture_on_commit_callbacks, celery_send_task):
    """``send(user, method, url, payload)`` with on_commit callbacks run and
    the Celery record reset, so only this request's tasks are asserted."""

    def _send(user, method, url, payload):
        celery_send_task.reset_mock()
        with django_capture_on_commit_callbacks(execute=True):
            return getattr(client_as(user), method)(url, payload, format='json')

    return _send


def _create(send, owner, payload):
    response = send(owner, 'post', URL, payload)
    assert response.status_code == 201, response.data
    return RealtimeSubmission.objects.get(pk=response.data['id'])


def _uploads(submission):
    return submission.history.filter(event_type=RealtimeSubmissionHistory.EVENT_UPLOADED).count()


def test_protocol_change_rejected_when_endpoints_do_not_fit(org, owner, send, celery_send_task):
    rts = _create(send, owner, _gtfs_rt_payload(org, _static_submission(org, owner)))

    response = send(
        owner, 'patch', f'{URL}{rts.id}/', {'protocol': 'gbfs', 'static_submission': None}
    )

    assert response.status_code == 400
    assert response.data['protocol'][0].code == 'realtime.endpoints_invalid_for_protocol'
    rts.refresh_from_db()
    assert rts.protocol == 'gtfs_rt'
    assert rts.static_submission_id is not None
    assert _validation_queued_for(celery_send_task) == []


def test_protocol_change_with_fitting_endpoints_queues_validation(
    org, owner, send, celery_send_task
):
    # Endpoint types of different protocols never overlap, so a valid switch
    # sends endpoints for the new protocol along with it.
    rts = _create(send, owner, _gtfs_rt_payload(org, _static_submission(org, owner)))
    gbfs = _gbfs_payload(org)

    response = send(
        owner,
        'patch',
        f'{URL}{rts.id}/',
        {'protocol': 'gbfs', 'static_submission': None, 'endpoints': gbfs['endpoints']},
    )

    assert response.status_code == 200, response.data
    rts.refresh_from_db()
    assert rts.protocol == 'gbfs'
    assert rts.static_submission_id is None
    assert [e.endpoint_type for e in rts.endpoints.all()] == ['gbfs']
    assert _validation_queued_for(celery_send_task) == [rts.id]
    assert _uploads(rts) == 2


def test_static_submission_change_queues_validation(org, owner, send, celery_send_task):
    rts = _create(send, owner, _gtfs_rt_payload(org, _static_submission(org, owner)))
    other_static = _static_submission(org, owner)

    response = send(owner, 'patch', f'{URL}{rts.id}/', {'static_submission': other_static.id})

    assert response.status_code == 200, response.data
    rts.refresh_from_db()
    assert rts.static_submission_id == other_static.id
    assert _validation_queued_for(celery_send_task) == [rts.id]
    assert _uploads(rts) == 2


def test_unchanged_protocol_and_static_queue_nothing(org, owner, send, celery_send_task):
    static = _static_submission(org, owner)
    rts = _create(send, owner, _gtfs_rt_payload(org, static))

    response = send(
        owner,
        'patch',
        f'{URL}{rts.id}/',
        {'protocol': 'gtfs_rt', 'static_submission': static.id, 'note': 'updated'},
    )

    assert response.status_code == 200, response.data
    assert _validation_queued_for(celery_send_task) == []
    assert _uploads(rts) == 1


def test_gbfs_name_change_queues_nothing(org, owner, send, celery_send_task):
    rts = _create(send, owner, _gbfs_payload(org))

    response = send(owner, 'patch', f'{URL}{rts.id}/', {'name': 'Renamed'})

    assert response.status_code == 200, response.data
    assert _validation_queued_for(celery_send_task) == []
