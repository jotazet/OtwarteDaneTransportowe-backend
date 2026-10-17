"""GTFS-RT entity types recorded from fetched bodies: a feed may mix trip
updates, vehicle positions and alerts under one endpoint."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, FeedSubmissionHistory, RealtimeSubmission
from data_manager.scheduler import _fetch_realtime_endpoint_rt
from data_manager.tasks import validate_realtime_submission_task
from data_manager.tests.test_flow import fake_download, gtfs_rt_feed_bytes
from data_manager.tests.test_realtime_rejection_reasons import make_rts, response
from data_manager.validators import validate_realtime_feed_content

pytestmark = pytest.mark.django_db


def trip_update_endpoint(*, hide_original=False):
    org = TransportOrganization.objects.create(region='Olkusz', transport_organization='ZKG')
    static = FeedSubmission.objects.create(transport_organization=org, data_type='gtfs')
    FeedSubmissionHistory.objects.create(
        submission=static,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
    )
    rts = make_rts(
        org,
        None,
        RealtimeSubmission.PROTOCOL_GTFS_RT,
        static,
        endpoint_types=['trip_update'],
        hide_original=hide_original,
    )
    return rts.endpoints.get()


@pytest.mark.parametrize(
    'entities, expected',
    [
        ((), set()),
        (('trip_update',), {'trip_update'}),
        (('trip_update', 'vehicle', 'alert'), {'trip_update', 'vehicle_position', 'service_alert'}),
    ],
)
def test_gtfs_rt_body_reports_the_endpoint_types_it_holds(entities, expected):
    body = gtfs_rt_feed_bytes(entities=entities)
    assert validate_realtime_feed_content('gtfs_rt', body) == expected


def test_proxied_fetch_adds_types_and_never_drops_one(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    endpoint = trip_update_endpoint(hide_original=True)
    bodies = (
        gtfs_rt_feed_bytes(entities=('trip_update', 'alert')),
        gtfs_rt_feed_bytes(entities=('trip_update', 'vehicle')),
        b'',
    )
    for body in bodies:
        monkeypatch.setattr('data_manager.scheduler.safe_download', fake_download(body))
        _fetch_realtime_endpoint_rt(endpoint, timezone.now())

    endpoint.refresh_from_db()
    # The second body has no alert and the empty one is rejected: nothing seen is dropped.
    assert endpoint.entity_types == ['service_alert', 'trip_update', 'vehicle_position']


def test_verification_records_types_of_an_endpoint_odt_does_not_proxy(fake_dns):
    endpoint = trip_update_endpoint()
    body = gtfs_rt_feed_bytes(entities=('trip_update', 'vehicle'))
    with patch('data_manager.net_security.safe_get', return_value=response(200, body)):
        result = validate_realtime_submission_task(endpoint.submission_id)

    assert result['status'] == 'ok', result
    endpoint.refresh_from_db()
    assert endpoint.entity_types == ['trip_update', 'vehicle_position']
