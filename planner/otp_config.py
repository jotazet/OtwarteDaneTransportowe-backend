"""OTP configuration files, generated from the ODT catalogue for every graph build.

OTP reads build-config.json when it builds a graph and otp-config.json plus
router-config.json when it serves one; nothing here is edited by hand.
"""

from collections import defaultdict
from itertools import product

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
# Every published feed is Polish; OTP needs one zone for the whole transit model.
TRANSIT_TIME_ZONE = 'Europe/Warsaw'


def build_config(feed_files: dict[str, str], *, osm_file: str | None = None) -> dict:
    """build-config.json for feed_id -> file name (in the build directory).

    Without ``osm_file`` OTP builds transit data only, which is how a single
    feed is tested before the full build.
    """
    return {
        'transitModelTimeZone': TRANSIT_TIME_ZONE,
        'transitServiceStart': TRANSIT_SERVICE_START,
        'transitServiceEnd': TRANSIT_SERVICE_END,
        'osm': [{'source': osm_file, 'timeZone': TRANSIT_TIME_ZONE}] if osm_file else [],
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


def otp_link(submission_id: int, endpoint) -> tuple[str, dict]:
    """(url, headers) by which OTP reads a catalogue endpoint, as the public catalogue links it.

    Proxied endpoints come from this app's cached copy over the internal network
    with the token header; the others from the provider, checked like any
    outbound URL first (raises OutboundURLBlocked), credentials moved to headers.
    """
    if endpoint.hide_original:
        # OTP substitutes ${...} from its environment, so the token itself never
        # lands in the generated file.
        return (
            f'{settings.PLANNER_ODT_INTERNAL_URL}/feed/rt/{submission_id}/{endpoint.endpoint_type}/',
            {INTERNAL_TOKEN_HEADER: '${PLANNER_INTERNAL_TOKEN}'},
        )
    assert_safe_outbound_url(endpoint.url)
    return split_userinfo(endpoint.url)


def realtime_updaters(planner_feeds) -> tuple[list[dict], list[dict]]:
    """OTP updaters for the GTFS-RT endpoints of the given feeds.

    OTP reads the same links the public catalogue shows: proxied endpoints from
    this app's cached copy (over the internal network, with the token header),
    the others from the provider's URL. Those URLs are checked like any other
    outbound URL first; a blocked one is skipped. A feed with ``realtime_from``
    gets the GTFS-RT published for that feed instead of its own. Returns
    ``(updaters, skipped)`` where ``skipped`` lists ``{feed_id, endpoint_type, error_code}``.
    """
    feed_ids = defaultdict(list)
    for feed in planner_feeds:
        feed_ids[feed.realtime_from_id or feed.submission_id].append(feed.feed_id)
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
        for feed_id, endpoint in product(feed_ids[rts.static_submission_id], rts.endpoints.all()):
            updater_type = UPDATER_TYPES.get(endpoint.endpoint_type)
            if updater_type is None:
                continue
            updater = {
                'type': updater_type,
                'feedId': feed_id,
                'frequency': f'PT{endpoint.interval}S',
            }
            try:
                url, headers = otp_link(rts.id, endpoint)
            except OutboundURLBlocked:
                skipped.append(
                    {
                        'feed_id': feed_id,
                        'endpoint_type': endpoint.endpoint_type,
                        'error_code': 'planner.rt_url_blocked',
                    }
                )
                continue
            updater['url'] = url
            if headers:
                updater['headers'] = headers
            updaters.append(updater)
    return updaters, skipped


def rental_updaters(sources) -> tuple[list[dict], list[dict]]:
    """OTP vehicle-rental updaters for enabled rental sources, and one result per source.

    OTP reads gbfs.json from the catalogue's link (proxied over the internal
    network, like GTFS-RT) and then follows the provider's own links inside it,
    so a source that needs credentials cannot work and is skipped.
    """
    published = set(completed_realtime_submission_ids())
    updaters, results = [], []
    for source in sources:
        result = {
            'rental_source': source.id,
            'network': source.network,
            'submission': source.submission_id,
            'outcome': 'skipped',
            'error_code': '',
        }
        results.append(result)
        endpoint = next(
            (e for e in source.submission.endpoints.all() if e.endpoint_type == 'gbfs'), None
        )
        if source.submission_id not in published:
            result['error_code'] = 'planner.rental_source_not_published'
            continue
        if endpoint is None:
            result['error_code'] = 'planner.rental_source_without_gbfs'
            continue
        if gbfs_needs_credentials(endpoint):
            result['error_code'] = 'planner.rental_source_requires_auth'
            continue
        try:
            url, headers = otp_link(source.submission_id, endpoint)
        except OutboundURLBlocked:
            result['error_code'] = 'planner.rt_url_blocked'
            continue
        updater = {
            'type': 'vehicle-rental',
            'sourceType': 'gbfs',
            'network': source.network,
            'frequency': f'PT{endpoint.interval}S',
            'url': url,
        }
        if headers:  # only the internal token: credentials were ruled out above
            updater['headers'] = headers
        updaters.append(updater)
        result['outcome'] = 'included'
    return updaters, results


def gbfs_needs_credentials(endpoint) -> bool:
    """OTP would need them for the files gbfs.json links to as well, which it cannot have."""
    return bool(endpoint.auth_type) or bool(split_userinfo(endpoint.url)[1])
