"""Generated OTP configuration and the internal realtime access it relies on."""

import json

import pytest
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from data_manager.api.views import RealtimePublicFeedDownloadView
from data_manager.models import (
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)
from OtwarteDaneTransportowe.throttling import ScopedRateThrottle
from planner import otp_config

pytestmark = pytest.mark.django_db


def make_realtime(feed, endpoints, *, published=True):
    rts = RealtimeSubmission.objects.create(
        transport_organization=feed.submission.transport_organization,
        protocol=RealtimeSubmission.PROTOCOL_GTFS_RT,
        static_submission=feed.submission,
    )
    for endpoint_type, url, hide_original in endpoints:
        RealtimeEndpointRT.objects.create(
            submission=rts,
            endpoint_type=endpoint_type,
            url=url,
            hide_original=hide_original,
            interval=40,
        )
    if published:
        RealtimeSubmissionHistory.objects.create(
            submission=rts,
            event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
            stage_before=3,
            stage_after=4,
        )
    return rts


def test_proxied_endpoint_is_read_from_this_app_with_the_token_placeholder(
    make_feed, settings, fake_dns
):
    settings.PLANNER_ODT_INTERNAL_URL = 'http://odt-web:8000'
    settings.PLANNER_INTERNAL_TOKEN = 'secret-token'
    feed = make_feed('krk')
    rts = make_realtime(feed, [('trip_update', 'https://src.example.org/tu.pb', True)])

    updaters, skipped = otp_config.realtime_updaters([feed])

    assert skipped == []
    assert updaters == [
        {
            'type': 'stop-time-updater',
            'feedId': 'krk',
            'frequency': 'PT40S',
            'url': f'http://odt-web:8000/feed/rt/{rts.id}/trip_update/',
            'headers': {'X-ODT-Internal-Token': '${PLANNER_INTERNAL_TOKEN}'},
        }
    ]
    assert 'secret-token' not in json.dumps(otp_config.router_config(updaters))


def test_original_endpoints_are_polled_directly(make_feed, fake_dns):
    feed = make_feed('krk')
    make_realtime(
        feed,
        [
            ('vehicle_position', 'https://src.example.org/vp.pb', False),
            ('service_alert', 'https://user:p%40ss@src.example.org/alerts.pb', False),
        ],
    )

    updaters, _ = otp_config.realtime_updaters([feed])

    by_type = {u['type']: u for u in updaters}
    assert by_type['vehicle-positions']['url'] == 'https://src.example.org/vp.pb'
    assert 'headers' not in by_type['vehicle-positions']
    alerts = by_type['real-time-alerts']
    assert alerts['url'] == 'https://src.example.org/alerts.pb'
    assert alerts['headers']['Authorization'].startswith('Basic ')


def test_endpoint_resolving_to_a_private_address_is_skipped(make_feed, fake_dns):
    fake_dns['intranet.example.org'] = '10.0.0.5'
    feed = make_feed('krk')
    make_realtime(feed, [('trip_update', 'http://intranet.example.org/tu.pb', False)])

    updaters, skipped = otp_config.realtime_updaters([feed])

    assert updaters == []
    assert skipped == [
        {'feed_id': 'krk', 'endpoint_type': 'trip_update', 'error_code': 'planner.rt_url_blocked'}
    ]


def test_feed_with_realtime_from_polls_that_feeds_realtime_instead(make_feed, fake_dns):
    original = make_feed('olkusz')
    make_realtime(original, [('trip_update', 'https://src.example.org/tu.pb', False)])
    shaped = make_feed('olkusz-shaped', org=original.submission.transport_organization)
    make_realtime(shaped, [('trip_update', 'https://src.example.org/own.pb', False)])
    shaped.realtime_from = original.submission

    updaters, _ = otp_config.realtime_updaters([original, shaped])

    assert sorted((u['feedId'], u['url']) for u in updaters) == [
        ('olkusz', 'https://src.example.org/tu.pb'),
        ('olkusz-shaped', 'https://src.example.org/tu.pb'),
    ]


def test_unpublished_realtime_is_ignored(make_feed, fake_dns):
    feed = make_feed('krk')
    make_realtime(feed, [('trip_update', 'https://src.example.org/tu.pb', False)], published=False)

    assert otp_config.realtime_updaters([feed]) == ([], [])


def test_single_feed_test_config_has_no_street_map():
    config = otp_config.build_config({'krk': 'feed.zip'})

    assert config['osm'] == []
    assert config['transitFeeds'] == [{'type': 'gtfs', 'feedId': 'krk', 'source': 'feed.zip'}]


def test_serving_config_turns_off_debug_and_transmodel():
    features = otp_config.otp_config()['otpFeatures']

    assert features['DebugUi'] is False
    assert features['TransmodelGraphQlApi'] is False
    assert features['ActuatorAPI'] is True


def _throttles(token_sent):
    headers = {otp_config.INTERNAL_TOKEN_HEADER: token_sent} if token_sent is not None else {}
    view = RealtimePublicFeedDownloadView()
    view.throttle_classes = [ScopedRateThrottle]
    view.request = Request(APIRequestFactory().get('/feed/rt/1/trip_update/', headers=headers))
    return view.get_throttles()


@pytest.mark.parametrize(
    ('configured', 'sent', 'throttled'),
    [
        ('secret-token', 'secret-token', False),
        ('secret-token', 'wrong', True),
        ('secret-token', None, True),
        ('', '', True),
    ],
)
def test_only_the_planner_token_skips_realtime_download_throttling(
    settings, configured, sent, throttled
):
    settings.PLANNER_INTERNAL_TOKEN = configured

    assert bool(_throttles(sent)) is throttled
