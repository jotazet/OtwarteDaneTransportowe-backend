"""The planner's search index: agencies, lines and stops of the served graph.

Copied from OTP after every activation and replaced as a whole, so stop and
line search keep answering from Postgres while OTP is stopped for a build.
OTP's own name search is prefix-only and does not fold Polish letters (ADR 0008).
"""

import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from itertools import islice

from django.db import transaction
from django.utils import timezone

from planner import otp_runtime
from planner.models import (
    GraphBuild,
    IndexedAgency,
    IndexedBlockTrip,
    IndexedRoute,
    IndexedStop,
    IndexedStopGroup,
    PlannerFeed,
)

logger = logging.getLogger(__name__)

INDEX_DOCUMENT = """
{
  feeds { feedId agencies { gtfsId name url } }
  routes { gtfsId shortName longName mode color textColor agency { gtfsId } }
  stops {
    gtfsId name code platformCode lat lon vehicleMode
    parentStation { gtfsId }
    routes { gtfsId }
  }
}
"""
# Per feed, to keep each answer small: Kraków has ~83k trips (0.2 s).
BLOCK_TRIPS_DOCUMENT = """
query BlockTrips($feeds: [String]) { trips(feeds: $feeds) { gtfsId blockId } }
"""
# All of Poland is ~93k stops (~20 MB of JSON); Małopolska answered in 0.15 s.
INDEX_TIMEOUT_SECONDS = 120
# Poles with the same name this close to each other are one place in search.
GROUP_RADIUS_M = 400
EARTH_RADIUS_M = 6_371_000
BATCH_SIZE = 5000


def search_text(value: str) -> str:
    """Lowercase words without diacritics: 'Kraków Główny' -> 'krakow glowny'."""
    # 'ł' has no Unicode decomposition, so NFKD alone would keep it.
    value = unicodedata.normalize('NFKD', value.lower().replace('ł', 'l'))
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return ' '.join(re.sub(r'[\W_]+', ' ', value).split())


def refresh_active() -> bool:
    """Index the active graph if it is not indexed yet; True when it did.

    Raises otp_runtime.OtpUnavailable when OTP does not answer; the old index
    then stays and the watchdog tries again.
    """
    build = GraphBuild.objects.filter(is_active=True, indexed_at__isnull=True).first()
    if build is None:
        return False
    data = otp_runtime.query(INDEX_DOCUMENT, timeout=INDEX_TIMEOUT_SECONDS)
    with transaction.atomic():
        counts = _replace(data)
        counts['block_trips'] = _replace_block_trips(
            [feed['feedId'] for feed in data.get('feeds') or []]
        )
        GraphBuild.objects.filter(pk=build.pk).update(indexed_at=timezone.now())
    logger.info('Planner search index refreshed for build %s: %s', build.id, counts)
    return True


def _replace(data: dict) -> dict:
    # Children first, so the cascades find nothing left to delete.
    IndexedStop.objects.all().delete()
    IndexedStopGroup.objects.all().delete()
    IndexedRoute.objects.all().delete()
    IndexedAgency.objects.all().delete()

    organizations = dict(
        PlannerFeed.objects.values_list('feed_id', 'submission__transport_organization_id')
    )
    agencies = IndexedAgency.objects.bulk_create(
        [
            IndexedAgency(
                agency_id=agency['gtfsId'],
                feed_id=feed['feedId'],
                name=agency.get('name') or '',
                url=agency.get('url') or '',
                organization_id=organizations.get(feed['feedId']),
            )
            for feed in data.get('feeds') or []
            for agency in feed.get('agencies') or []
        ],
        batch_size=BATCH_SIZE,
    )
    agency_pks = {agency.agency_id: agency.pk for agency in agencies}

    # Stops no line calls at cannot start or end a trip.
    stops = [stop for stop in data.get('stops') or [] if stop.get('routes')]
    centres = _route_centres(stops)
    route_agency = {
        route['gtfsId']: route['agency']['gtfsId']
        for route in data.get('routes') or []
        if (route.get('agency') or {}).get('gtfsId') in agency_pks
    }
    routes = [
        IndexedRoute(
            route_id=route['gtfsId'],
            agency_id=agency_pks[route_agency[route['gtfsId']]],
            short_name=route.get('shortName') or '',
            long_name=route.get('longName') or '',
            mode=(route.get('mode') or '').lower(),
            color=route.get('color') or '',
            text_color=route.get('textColor') or '',
            sort_key=sort_key(route.get('shortName') or '', route.get('longName') or ''),
            short_name_search=search_text(route.get('shortName') or ''),
            name_search=search_text(
                f"{route.get('shortName') or ''} {route.get('longName') or ''}"
            ),
            lat=centres.get(route['gtfsId'], (None, None))[0],
            lon=centres.get(route['gtfsId'], (None, None))[1],
        )
        for route in data.get('routes') or []
        if route['gtfsId'] in route_agency
    ]
    IndexedRoute.objects.bulk_create(routes, batch_size=BATCH_SIZE)

    groups, members = [], []
    for cluster in _clusters(stops):
        route_ids = sorted({r['gtfsId'] for stop in cluster for r in stop.get('routes') or []})
        name = Counter(stop['name'] for stop in cluster).most_common(1)[0][0]
        groups.append(
            IndexedStopGroup(
                name=name,
                name_search=search_text(name),
                lat=sum(stop['lat'] for stop in cluster) / len(cluster),
                lon=sum(stop['lon'] for stop in cluster) / len(cluster),
                modes=sorted(
                    {stop['vehicleMode'].lower() for stop in cluster if stop.get('vehicleMode')}
                ),
                agency_ids=sorted({route_agency[r] for r in route_ids if r in route_agency}),
                route_ids=route_ids,
                route_count=len(route_ids),
            )
        )
        members.append(cluster)
    IndexedStopGroup.objects.bulk_create(groups, batch_size=BATCH_SIZE)
    IndexedStop.objects.bulk_create(
        [
            IndexedStop(
                stop_id=stop['gtfsId'],
                group_id=group.pk,
                name=stop['name'],
                code=stop.get('code') or '',
                platform=stop.get('platformCode') or '',
                lat=stop['lat'],
                lon=stop['lon'],
                mode=(stop.get('vehicleMode') or '').lower(),
                station_id=(stop.get('parentStation') or {}).get('gtfsId') or '',
                route_ids=sorted(r['gtfsId'] for r in stop.get('routes') or []),
            )
            for group, cluster in zip(groups, members, strict=True)
            for stop in cluster
        ],
        batch_size=BATCH_SIZE,
    )
    return {
        'agencies': len(agencies),
        'routes': len(routes),
        'stops': len(stops),
        'groups': len(groups),
    }


def _replace_block_trips(feed_ids: list[str]) -> int:
    """Feed by feed and in batches: all of Poland has ~1.3M trips."""
    IndexedBlockTrip.objects.all().delete()
    count = 0
    for feed_id in feed_ids:
        trips = otp_runtime.query(
            BLOCK_TRIPS_DOCUMENT, {'feeds': [feed_id]}, timeout=INDEX_TIMEOUT_SECONDS
        ).get('trips')
        rows = (
            IndexedBlockTrip(feed_id=feed_id, block_id=trip['blockId'], trip_id=trip['gtfsId'])
            for trip in trips or []
            if trip.get('blockId')
        )
        while batch := list(islice(rows, BATCH_SIZE)):
            IndexedBlockTrip.objects.bulk_create(batch)
            count += len(batch)
    return count


def _route_centres(stops: list[dict]) -> dict[str, tuple[float, float]]:
    sums = defaultdict(lambda: [0.0, 0.0, 0])
    for stop in stops:
        for route in stop.get('routes') or []:
            total = sums[route['gtfsId']]
            total[0] += stop['lat']
            total[1] += stop['lon']
            total[2] += 1
    return {route: (lat / n, lon / n) for route, (lat, lon, n) in sums.items()}


def sort_key(short_name: str, long_name: str) -> str:
    """Numbers first and in numeric order ("2" < "10" < "52A"), then the rest by name."""
    match = re.match(r'(\d+)(.*)', short_name)
    if match:
        return f'0{int(match[1]):010d}{match[2].lower()}'[:80]
    return f'1{(short_name or long_name).lower()}'[:80]


def _clusters(stops: list[dict]) -> list[list[dict]]:
    """Stops with the same search name, joined while one is within GROUP_RADIUS_M of another."""
    by_name = defaultdict(list)
    for stop in stops:
        by_name[search_text(stop['name'])].append(stop)
    return [cluster for same_name in by_name.values() for cluster in _nearby(same_name)]


def _nearby(stops: list[dict]) -> list[list[dict]]:
    """Single-linkage clusters (union-find); only stops within the radius in latitude are compared."""
    stops = sorted(stops, key=lambda stop: stop['lat'])
    parent = list(range(len(stops)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    window = math.degrees(GROUP_RADIUS_M / EARTH_RADIUS_M)
    for i, a in enumerate(stops):
        for j in range(i + 1, len(stops)):
            b = stops[j]
            if b['lat'] - a['lat'] > window:
                break
            if distance_m(a['lat'], a['lon'], b['lat'], b['lon']) <= GROUP_RADIUS_M:
                parent[root(j)] = root(i)
    clusters = defaultdict(list)
    for i, stop in enumerate(stops):
        clusters[root(i)].append(stop)
    return list(clusters.values())


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance; accurate to well under 1% over a few tens of km."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return EARTH_RADIUS_M * math.hypot(x, y)
