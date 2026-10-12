"""OTP configuration files, generated from the ODT catalogue for every graph build.

OTP reads build-config.json when it builds a graph and otp-config.json plus
router-config.json when it serves one; nothing here is edited by hand.
"""

from django.conf import settings

from data_manager.models import RealtimeSubmission, completed_realtime_submission_ids
from data_manager.net_security import OutboundURLBlocked, assert_safe_outbound_url, split_userinfo
from OtwarteDaneTransportowe.internal_requests import INTERNAL_TOKEN_HEADER

# Service days kept in the graph: feeds publish 1–8 weeks ahead, and a shorter
# window keeps the graph small enough for the VPS (docs/deployment.md, sizing).
TRANSIT_SERVICE_START = '-P1D'
TRANSIT_SERVICE_END = 'P2M'

UPDATER_TYPES = {
    'trip_update': 'stop-time-updater',
    'vehicle_position': 'vehicle-positions',
    'service_alert': 'real-time-alerts',
}


def build_config(feed_files: dict[str, str], *, osm_file: str | None = None) -> dict:
    """build-config.json for feed_id -> file name (in the build directory).

    Without ``osm_file`` OTP builds transit data only, which is how a single
    feed is tested before the full build.
    """
    return {
        'transitModelTimeZone': 'Europe/Warsaw',
        'transitServiceStart': TRANSIT_SERVICE_START,
        'transitServiceEnd': TRANSIT_SERVICE_END,
        'osm': [{'source': osm_file, 'timeZone': 'Europe/Warsaw'}] if osm_file else [],
        'dem': [],
        'transitFeeds': [
            {'type': 'gtfs', 'feedId': feed_id, 'source': name}
            for feed_id, name in sorted(feed_files.items())
        ],
    }


def otp_config() -> dict:
    """Only the GTFS GraphQL API and the health/metrics endpoints are switched on."""
    return {
        'otpFeatures': {
            'GtfsGraphQlApi': True,
            'TransmodelGraphQlApi': False,
            'DebugUi': False,
            'ActuatorAPI': True,
            'CostlyAssertions': False,
        }
    }


def router_config(updaters: list[dict]) -> dict:
    return {'server': {'apiProcessingTimeout': '15s'}, 'updaters': updaters}


def realtime_updaters(planner_feeds) -> tuple[list[dict], list[dict]]:
    """OTP updaters for the GTFS-RT endpoints of the given feeds.

    OTP reads the same links the public catalogue shows: proxied endpoints from
    this app's cached copy (over the internal network, with the token header),
    the others from the provider's URL. Those URLs are checked like any other
    outbound URL first; a blocked one is skipped. Returns ``(updaters, skipped)``
    where ``skipped`` lists ``{feed_id, endpoint_type, error_code}``.
    """
    feed_ids = {feed.submission_id: feed.feed_id for feed in planner_feeds}
    submissions = (
        RealtimeSubmission.objects.filter(
            static_submission_id__in=feed_ids,
            protocol=RealtimeSubmission.PROTOCOL_GTFS_RT,
            id__in=completed_realtime_submission_ids(),
        )
        .prefetch_related('endpoints')
        .order_by('id')
    )
    updaters, skipped = [], []
    for rts in submissions:
        feed_id = feed_ids[rts.static_submission_id]
        for endpoint in rts.endpoints.all():
            updater_type = UPDATER_TYPES.get(endpoint.endpoint_type)
            if updater_type is None:
                continue
            updater = {
                'type': updater_type,
                'feedId': feed_id,
                'frequency': f'PT{endpoint.interval}S',
            }
            if endpoint.hide_original:
                updater['url'] = (
                    f'{settings.PLANNER_ODT_INTERNAL_URL}/feed/rt/{rts.id}/{endpoint.endpoint_type}/'
                )
                # OTP substitutes ${...} from its environment, so the token
                # itself never lands in the generated file.
                updater['headers'] = {INTERNAL_TOKEN_HEADER: '${PLANNER_INTERNAL_TOKEN}'}
            else:
                try:
                    assert_safe_outbound_url(endpoint.url)
                except OutboundURLBlocked:
                    skipped.append(
                        {
                            'feed_id': feed_id,
                            'endpoint_type': endpoint.endpoint_type,
                            'error_code': 'planner.rt_url_blocked',
                        }
                    )
                    continue
                url, headers = split_userinfo(endpoint.url)
                updater['url'] = url
                if headers:
                    updater['headers'] = headers
            updaters.append(updater)
    return updaters, skipped
