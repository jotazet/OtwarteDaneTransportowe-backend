"""Vehicle positions: one OTP snapshot of every line, shared by the requests of a process.

OTP has positions only per line variant (Pattern.vehiclePositions) and keeps only
those it matched to a trip (docs/domain/journey-planner.md). The snapshot is
refreshed at most every SNAPSHOT_SECONDS per web process; filters apply to it.
"""

import threading
import time
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

from planner import otp_runtime, schedules
from planner.planning import utc
from planner.schedules import ROUTE_FIELDS, route_summary

VEHICLES_DOCUMENT = f"""
query Vehicles {{
  routes {{
    {ROUTE_FIELDS}
    patterns {{
      headsign
      vehiclePositions {{
        vehicleId label lat lon heading speed lastUpdate
        stopRelationship {{ status stop {{ gtfsId name }} }}
        trip {{ gtfsId tripHeadsign blockId }}
      }}
    }}
  }}
}}
"""
# Feeds update positions every 10-30 s; a request sees data at most this much older.
SNAPSHOT_SECONDS = 10
# A position not updated for this long belongs to a vehicle that stopped reporting.
MAX_POSITION_AGE = timedelta(minutes=5)
# While one request refreshes the snapshot, others wait this long, then get the
# previous snapshot if it is recent enough, or planner.busy: a slow OTP must not
# hold every thread (like otp_runtime's query slots).
REFRESH_WAIT_SECONDS = 1
STALE_SNAPSHOT_SECONDS = 60

_lock = threading.Lock()
_snapshot = {'until': 0.0, 'taken': None, 'vehicles': []}


def snapshot(query) -> list[dict]:
    """Every current vehicle; ``query`` runs the OTP document (refreshed once per process).

    Raises otp_runtime.OtpBusy when another request is refreshing and there is
    no recent snapshot, and whatever ``query`` raises.
    """
    if not _lock.acquire(timeout=REFRESH_WAIT_SECONDS):
        taken = _snapshot['taken']
        if taken is not None and time.monotonic() - taken < STALE_SNAPSHOT_SECONDS:
            return _snapshot['vehicles']
        raise otp_runtime.OtpBusy
    try:
        if time.monotonic() >= _snapshot['until']:
            _snapshot['vehicles'] = vehicles(query(VEHICLES_DOCUMENT, {}))
            _snapshot['taken'] = time.monotonic()
            _snapshot['until'] = _snapshot['taken'] + SNAPSHOT_SECONDS
        return _snapshot['vehicles']
    finally:
        _lock.release()


def forget_snapshot() -> None:
    with _lock:
        _snapshot.update(until=0.0, taken=None, vehicles=[])


def vehicles(data: dict) -> list[dict]:
    oldest = timezone.now() - MAX_POSITION_AGE
    rows = []
    for route in data.get('routes') or []:
        for pattern in route.get('patterns') or []:
            for position in pattern.get('vehiclePositions') or []:
                updated = position.get('lastUpdate')
                if updated and datetime.fromisoformat(updated) < oldest:
                    continue
                rows.append(_vehicle(route, pattern, position))
    rows.sort(key=lambda row: row['id'] or '')  # a feed may leave out the vehicle ID
    return rows


def _vehicle(route: dict, pattern: dict, position: dict) -> dict:
    trip = position.get('trip') or {}
    relationship = position.get('stopRelationship') or {}
    stop = relationship.get('stop')
    return {
        'id': position['vehicleId'],
        'label': position.get('label'),
        'lat': position['lat'],
        'lon': position['lon'],
        'heading': position.get('heading'),
        'speed_m_s': position.get('speed'),
        'updated_at': utc(position.get('lastUpdate')),
        'route': route_summary(route),
        'headsign': trip.get('tripHeadsign') or pattern.get('headsign'),
        'trip_id': trip.get('gtfsId'),
        'block_id': trip.get('blockId') or None,
        'stop_status': (relationship.get('status') or '').lower() or None,
        'stop': {'id': stop['gtfsId'], 'name': stop['name']} if stop else None,
    }


def matching(rows, bbox=None, modes=None, agency_ids=None, route_ids=None) -> list[dict]:
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        rows = [
            r for r in rows if min_lat <= r['lat'] <= max_lat and min_lon <= r['lon'] <= max_lon
        ]
    if modes:
        rows = [r for r in rows if r['route']['mode'] in modes]
    if agency_ids is not None:
        rows = [r for r in rows if (r['route']['agency'] or {}).get('id') in agency_ids]
    if route_ids:
        rows = [r for r in rows if r['route']['id'] in route_ids]
    return rows


# ---------------------------------------------------------------------------
# Coverage: how many of an operator's trips under way report a position
# ---------------------------------------------------------------------------

_RUN = """
  trip { gtfsId }
  realTimeTripState { canceled }
  start { schedule { time { ... on ArrivalDepartureTime { departure } } }
          realTime { departure { time } } }
  end { schedule { time { ... on ArrivalDepartureTime { arrival } } }
        realTime { arrival { time } } }
"""
# Yesterday's service day too: its trips after midnight are under way today.
RUNNING_DOCUMENT = f"""
query Running($agency: String!, $today: LocalDate!, $yesterday: LocalDate!) {{
  agency(id: $agency) {{
    gtfsId name
    routes {{
      patterns {{
        today: tripsOnServiceDate(serviceDate: $today) {{ {_RUN} }}
        yesterday: tripsOnServiceDate(serviceDate: $yesterday) {{ {_RUN} }}
      }}
    }}
  }}
}}
"""
# Every trip of the operator for two days (MPK Kraków: 33k trips, ~10 MB, 0.8 s),
# so the answer is reused for a minute, and while one request recounts, the
# others use the previous count.
RUNNING_CACHE_SECONDS = 60
RUNNING_PREVIOUS_SECONDS = 600


def running_trips(agency_id: str, graph_id: int, query) -> dict | None:
    """``{'agency': {id, name}, 'trips': [IDs of its trips under way]}``; None if unknown.

    Under way: started and not yet finished now, by predicted times where the
    operator gives them; trips cancelled in realtime are not counted. Cached per
    served graph, whose trip IDs they are.
    """
    key = f'planner:running:{graph_id}:{agency_id}'
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    previous = cache.get(f'{key}:previous')
    if previous is not None and not cache.add(f'{key}:lock', 1, RUNNING_CACHE_SECONDS):
        return previous or None
    try:
        today = schedules.today()
        data = query(
            RUNNING_DOCUMENT,
            {
                'agency': agency_id,
                'today': today.isoformat(),
                'yesterday': (today - timedelta(days=1)).isoformat(),
            },
        )
        agency = data.get('agency')
        result = (
            {
                'agency': {'id': agency['gtfsId'], 'name': agency['name']},
                'trips': sorted(_under_way(agency, timezone.now())),
            }
            if agency
            else {}
        )
        cache.set(key, result, RUNNING_CACHE_SECONDS)
        cache.set(f'{key}:previous', result, RUNNING_PREVIOUS_SECONDS)
    finally:
        cache.delete(f'{key}:lock')
    return result or None


def _under_way(agency: dict, now: datetime) -> set[str]:
    trips = set()
    for route in agency.get('routes') or []:
        for pattern in route.get('patterns') or []:
            for run in (pattern.get('today') or []) + (pattern.get('yesterday') or []):
                if (run.get('realTimeTripState') or {}).get('canceled'):
                    continue
                began = _time(run.get('start'), 'departure')
                ends = _time(run.get('end'), 'arrival')
                if began and ends and began <= now <= ends:
                    trips.add(run['trip']['gtfsId'])
    return trips


def _time(call: dict | None, which: str) -> datetime | None:
    call = call or {}
    value = ((call.get('realTime') or {}).get(which) or {}).get('time') or (
        (call.get('schedule') or {}).get('time') or {}
    ).get(which)
    return datetime.fromisoformat(value) if value else None
