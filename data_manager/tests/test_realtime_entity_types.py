"""GTFS-RT entity types recorded from fetched bodies: a feed may mix trip
updates, vehicle positions and alerts under one endpoint."""

import io
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.utils import timezone
from google.transit import gtfs_realtime_pb2

from cases.models import TransportOrganization
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)
from data_manager.scheduler import _fetch_realtime_endpoint_rt
from data_manager.tasks import validate_realtime_submission_task
from data_manager.validators import validate_realtime_feed_content

pytestmark = pytest.mark.django_db


def feed_bytes(*fields: str) -> bytes:
    """A GTFS-RT FeedMessage with one entity per given FeedEntity field."""
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = '2.0'
    for number, field in enumerate(fields):
        entity = message.entity.add(id=str(number))
        if field == 'trip_update':
            entity.trip_update.trip.trip_id = 't1'
        elif field == 'vehicle':
            entity.vehicle.position.latitude = 50.28
            entity.vehicle.position.longitude = 19.56
        else:
            entity.alert.header_text.translation.add(text='x')
    return message.SerializeToString()


class _Response:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


def fake_download(body: bytes):
    @contextmanager
    def _cm(*args, **kwargs):
        yield _Response(body), io.BytesIO(body)

    return _cm


@pytest.fixture
def endpoint(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    org = TransportOrganization.objects.create(region='Olkusz', transport_organization='ZKG')
    static = FeedSubmission.objects.create(transport_organization=org, data_type='gtfs')
    FeedSubmissionHistory.objects.create(
        submission=static,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
    )
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        protocol=RealtimeSubmission.PROTOCOL_GTFS_RT,
        static_submission=static,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
    )
    return RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='trip_update',
        url='https://example.org/rt/gtfsrt.pb',
        interval=15,
    )


@pytest.mark.parametrize(
    'fields, expected',
    [
        ((), set()),
        (('trip_update',), {'trip_update'}),
        (('trip_update', 'vehicle', 'alert'), {'trip_update', 'vehicle_position', 'service_alert'}),
    ],
)
def test_gtfs_rt_body_reports_the_endpoint_types_it_holds(fields, expected):
    assert validate_realtime_feed_content('gtfs_rt', feed_bytes(*fields)) == expected


def test_proxied_fetch_adds_types_and_never_drops_one(endpoint, monkeypatch):
    endpoint.hide_original = True
    endpoint.save()

    for body in (feed_bytes('trip_update', 'alert'), feed_bytes('trip_update', 'vehicle'), b''):
        monkeypatch.setattr('data_manager.scheduler.safe_download', fake_download(body))
        _fetch_realtime_endpoint_rt(endpoint, timezone.now())

    endpoint.refresh_from_db()
    # The second body has no alert and the empty one is rejected: nothing seen is dropped.
    assert endpoint.entity_types == ['service_alert', 'trip_update', 'vehicle_position']


def test_verification_records_types_of_an_endpoint_odt_does_not_proxy(endpoint, fake_dns):
    with patch(
        'data_manager.net_security.safe_get',
        return_value=_Response(feed_bytes('trip_update', 'vehicle')),
    ):
        result = validate_realtime_submission_task(endpoint.submission_id)

    assert result['status'] == 'ok', result
    endpoint.refresh_from_db()
    assert endpoint.entity_types == ['trip_update', 'vehicle_position']
