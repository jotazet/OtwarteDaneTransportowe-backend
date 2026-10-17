"""Departures, stop timetables, lines and trips: fixed OTP documents and their answers in our shape.

Like planning.py, client input only travels as GraphQL variables. Times are
UTC instants; a departure is OTP's service day plus its seconds, which stays
right across the DST change (docs/domain/journey-planner.md).
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from planner.otp_config import TRANSIT_TIME_ZONE
from planner.planning import CALL_TIMES_FRAGMENT, call_times
from planner.search_index import sort_key

ROUTE_FIELDS = 'gtfsId shortName longName mode color textColor agency { gtfsId name }'

# The next `count` departures per stop, or with a line filter per line variant
# (otherwise other lines could take all `count` places).
DEPARTURES_DOCUMENT = f"""
query Departures(
  $ids: [String], $start: Long!, $range: Int!, $count: Int!, $byVariant: Boolean!
) {{
  stops(ids: $ids) {{
    gtfsId
    stoptimesWithoutPatterns(
      startTime: $start
      timeRange: $range
      numberOfDepartures: $count
      omitNonPickups: true
      omitCanceled: false
    ) @skip(if: $byVariant) {{ ...Departure }}
    stoptimesForPatterns(
      startTime: $start
      timeRange: $range
      numberOfDepartures: $count
      omitNonPickups: true
      omitCanceled: false
    ) @include(if: $byVariant) {{ stoptimes {{ ...Departure }} }}
  }}
}}

fragment Departure on Stoptime {{
  scheduledDeparture realtimeDeparture departureDelay realtime realtimeState
  serviceDay headsign
  stop {{ platformCode }}
  trip {{ gtfsId tripHeadsign route {{ {ROUTE_FIELDS} }} }}
}}
"""

STOP_TIMETABLE_DOCUMENT = f"""
query StopTimetable($id: String!, $date: String!) {{
  serviceTimeRange {{ start end }}
  stop(id: $id) {{
    gtfsId name platformCode
    stoptimesForServiceDate(date: $date, omitNonPickups: true, omitCanceled: false) {{
      pattern {{ headsign route {{ {ROUTE_FIELDS} }} }}
      stoptimes {{ scheduledDeparture serviceDay realtimeState trip {{ gtfsId }} }}
    }}
  }}
}}
"""

ROUTE_DOCUMENT = f"""
query Route($id: String!, $withGeometry: Boolean!) {{
  route(id: $id) {{
    {ROUTE_FIELDS}
    patterns {{
      headsign directionId
      stops {{ gtfsId name lat lon platformCode }}
      patternGeometry @include(if: $withGeometry) {{ points }}
    }}
  }}
}}
"""

TRIP_DOCUMENT = f"""
query Trip($id: String!, $date: LocalDate!, $withGeometry: Boolean!) {{
  serviceTimeRange {{ start end }}
  trip(id: $id) {{
    gtfsId tripHeadsign directionId blockId
    route {{ {ROUTE_FIELDS} }}
    tripGeometry @include(if: $withGeometry) {{ points }}
    onServiceDate(date: $date) {{
      serviceDate
      stopCalls {{
        stopLocation {{ ... on Stop {{ gtfsId name lat lon platformCode }} }}
        ...CallTimes
      }}
    }}
  }}
}}
{CALL_TIMES_FRAGMENT}"""

# A block is one vehicle's trips; its trips of every day type are asked for at
# once (Kraków's largest: 366 trips, 21 ms). More means a feed misuses block_id.
MAX_BLOCK_TRIPS = 1000

# Whether yesterday's run of a trip is still under way (a trip after midnight).
CURRENT_RUN_DOCUMENT = """
query CurrentRun($id: String!, $yesterday: LocalDate!) {
  trip(id: $id) {
    onServiceDate(date: $yesterday) {
      end {
        schedule { time { ... on ArrivalDepartureTime { arrival } } }
        realTime { arrival { time } }
      }
    }
  }
}
"""

_ZONE = ZoneInfo(TRANSIT_TIME_ZONE)


def today() -> date:
    """The current calendar day in Poland, the default service date."""
    return datetime.now(_ZONE).date()


def current_service_date(data: dict, now: datetime) -> date:
    """Yesterday while yesterday's run of the trip has not ended (after midnight), else today.

    ``data`` answers CURRENT_RUN_DOCUMENT for yesterday.
    """
    end = (((data.get('trip') or {}).get('onServiceDate')) or {}).get('end') or {}
    predicted = ((end.get('realTime') or {}).get('arrival') or {}).get('time')
    scheduled = ((end.get('schedule') or {}).get('time') or {}).get('arrival')
    ends = predicted or scheduled
    day = today()
    if ends and datetime.fromisoformat(ends) >= now:
        return day - timedelta(days=1)
    return day


# ---------------------------------------------------------------------------
# Departures
# ---------------------------------------------------------------------------


def departures_variables(
    stop_ids, start: datetime, window_minutes: int, limit: int, by_variant: bool
) -> dict:
    return {
        'ids': stop_ids,
        'start': int(start.timestamp()),
        'range': window_minutes * 60,
        # Per stop (or variant); the merged list is cut to `limit` afterwards.
        'count': limit,
        'byVariant': by_variant,
    }


def departures(data: dict, limit: int, route_ids=None) -> list[dict] | None:
    """Departures of every stop, soonest first; None when none of the stops exists."""
    stops = [stop for stop in data.get('stops') or [] if stop]
    if not stops:
        return None
    rows = []
    for stop in stops:
        calls = list(stop.get('stoptimesWithoutPatterns') or [])
        for group in stop.get('stoptimesForPatterns') or []:
            calls += group.get('stoptimes') or []
        rows += [
            _departure(stop['gtfsId'], call)
            for call in calls
            if not route_ids or call['trip']['route']['gtfsId'] in route_ids
        ]
    rows.sort(key=lambda row: (row['estimated'] or row['scheduled'], row['stop_id']))
    return rows[:limit]


def _departure(stop_id: str, call: dict) -> dict:
    realtime = bool(call.get('realtime'))
    trip = call['trip']
    return {
        'stop_id': stop_id,
        'platform': (call.get('stop') or {}).get('platformCode'),
        'route': route_summary(trip['route']),
        'trip_id': trip['gtfsId'],
        'service_date': service_date(call['serviceDay']),
        'headsign': call.get('headsign') or trip.get('tripHeadsign'),
        'scheduled': instant(call['serviceDay'], call['scheduledDeparture']),
        'estimated': instant(call['serviceDay'], call['realtimeDeparture']) if realtime else None,
        'delay_s': call.get('departureDelay') if realtime else None,
        'realtime': realtime,
        'status': (call.get('realtimeState') or 'SCHEDULED').lower(),
    }


# ---------------------------------------------------------------------------
# Stop timetable
# ---------------------------------------------------------------------------


def stop_timetable(data: dict, route_ids=None) -> dict | None:
    """The day's departures of a stop by line and headsign; None for an unknown stop."""
    stop = data.get('stop')
    if stop is None:
        return None
    lines = {}
    for group in stop.get('stoptimesForServiceDate') or []:
        route = group['pattern']['route']
        if route_ids and route['gtfsId'] not in route_ids:
            continue
        headsign = group['pattern'].get('headsign')
        line = lines.setdefault(
            (route['gtfsId'], headsign),
            {'route': route_summary(route), 'headsign': headsign, 'departures': []},
        )
        line['departures'] += [
            {
                'time': instant(call['serviceDay'], call['scheduledDeparture']),
                'trip_id': (call.get('trip') or {}).get('gtfsId'),
                'canceled': call.get('realtimeState') == 'CANCELED',
            }
            for call in group.get('stoptimes') or []
        ]
    for line in lines.values():
        line['departures'].sort(key=lambda row: row['time'])
    return {
        'stop': {'id': stop['gtfsId'], 'name': stop['name'], 'platform': stop.get('platformCode')},
        'lines': sorted(
            lines.values(),
            key=lambda line: (
                sort_key(line['route']['short_name'] or '', line['route']['long_name'] or ''),
                line['headsign'] or '',
            ),
        ),
    }


# ---------------------------------------------------------------------------
# Line and trip
# ---------------------------------------------------------------------------


def block_document(trip_count: int) -> str:
    """One alias per trip (OTP has no query for several trips); IDs stay variables."""
    ids = ', '.join(f'$t{n}: String!' for n in range(trip_count))
    trips = '\n'.join(f'  t{n}: trip(id: $t{n}) {{ ...BlockTrip }}' for n in range(trip_count))
    return f"""
query Block($date: LocalDate!, {ids}) {{
  serviceTimeRange {{ start end }}
{trips}
}}

fragment BlockTrip on Trip {{
  gtfsId tripHeadsign
  route {{ {ROUTE_FIELDS} }}
  onServiceDate(date: $date) {{ start {{ ...BlockCall }} end {{ ...BlockCall }} }}
}}

fragment BlockCall on StopCall {{
  stopLocation {{ ... on Stop {{ gtfsId name }} }}
  ...CallTimes
}}
{CALL_TIMES_FRAGMENT}"""


def block_variables(trip_ids: list[str], day: date) -> dict:
    return {'date': day.isoformat(), **{f't{n}': trip_id for n, trip_id in enumerate(trip_ids)}}


def block_trips(data: dict, trip_count: int, now: datetime) -> list[dict]:
    """The block's trips running on the date, in order; ``current`` marks the one under way."""
    trips = []
    for n in range(trip_count):
        trip = data.get(f't{n}')
        on_date = (trip or {}).get('onServiceDate')
        if not on_date:
            continue
        start = _block_call(on_date['start'], 'departure')
        end = _block_call(on_date['end'], 'arrival')
        trips.append(
            {
                'id': trip['gtfsId'],
                'route': route_summary(trip['route']),
                'headsign': trip.get('tripHeadsign'),
                'start': start,
                'end': end,
                'realtime': bool(start['estimated'] or end['estimated']),
            }
        )
    trips.sort(key=lambda trip: trip['start']['scheduled'] or '')
    for trip in trips:
        began = trip['start']['estimated'] or trip['start']['scheduled']
        ends = trip['end']['estimated'] or trip['end']['scheduled']
        trip['current'] = bool(
            began and ends and datetime.fromisoformat(began) <= now <= datetime.fromisoformat(ends)
        )
    return trips


def _block_call(call: dict, which: str) -> dict:
    return {'stop': _stop(call.get('stopLocation') or {}), **call_times(call)[which]}


def route_detail(data: dict) -> dict | None:
    route = data.get('route')
    if route is None:
        return None
    variants = [
        {
            'headsign': pattern.get('headsign'),
            # OTP writes -1 for "direction irrelevant"; GTFS (and the API) have none.
            'direction_id': pattern.get('directionId')
            if pattern.get('directionId') in (0, 1)
            else None,
            'stops': [_stop(stop) for stop in pattern.get('stops') or []],
            'geometry': (pattern.get('patternGeometry') or {}).get('points'),
        }
        for pattern in route.get('patterns') or []
    ]
    # Per direction, the variant with the most stops (usually the main one) first.
    variants.sort(key=lambda v: (v['direction_id'] is None, v['direction_id'], -len(v['stops'])))
    return {**route_summary(route), 'variants': variants}


def trip_detail(data: dict) -> dict | None:
    """The trip on its service date; None when it does not exist.

    ``calls`` is None when the trip does not run on that date.
    """
    trip = data.get('trip')
    if trip is None:
        return None
    on_date = trip.get('onServiceDate')
    direction = trip.get('directionId')
    calls = [_call(call) for call in on_date['stopCalls']] if on_date else None
    return {
        'id': trip['gtfsId'],
        'headsign': trip.get('tripHeadsign'),
        'direction_id': int(direction) if direction not in (None, '') else None,
        'block_id': trip.get('blockId') or None,
        'route': route_summary(trip['route']),
        'service_date': on_date['serviceDate'] if on_date else None,
        'realtime': any(
            call['departure']['estimated'] or call['arrival']['estimated'] for call in calls or []
        ),
        'calls': calls,
        'geometry': (trip.get('tripGeometry') or {}).get('points'),
    }


def _call(call: dict) -> dict:
    return {'stop': _stop(call.get('stopLocation') or {}), **call_times(call)}


def _stop(stop: dict) -> dict:
    return {
        'id': stop.get('gtfsId'),
        'name': stop.get('name'),
        'lat': stop.get('lat'),
        'lon': stop.get('lon'),
        'platform': stop.get('platformCode'),
    }


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def service_period(data: dict) -> tuple[date, date] | None:
    """First and last service date the served graph has timetables for."""
    period = data.get('serviceTimeRange') or {}
    if period.get('start') is None or period.get('end') is None:
        return None
    first = datetime.fromtimestamp(period['start'], _ZONE).date()
    # `end` is the start of the day after the last one.
    last = datetime.fromtimestamp(period['end'], _ZONE).date() - timedelta(days=1)
    return first, last


def route_summary(route: dict) -> dict:
    agency = route.get('agency')
    return {
        'id': route['gtfsId'],
        'short_name': route.get('shortName'),
        'long_name': route.get('longName'),
        'mode': (route.get('mode') or '').lower(),
        'color': route.get('color'),
        'text_color': route.get('textColor'),
        'agency': {'id': agency['gtfsId'], 'name': agency['name']} if agency else None,
    }


def instant(service_day: int, seconds: int) -> str:
    """OTP's service day (epoch seconds) plus seconds into it, as a UTC instant."""
    return (datetime.fromtimestamp(service_day + seconds, UTC)).isoformat().replace('+00:00', 'Z')


def service_date(service_day: int) -> str:
    """The calendar date of a service day.

    OTP's service day starts 12 hours before local noon (GTFS), which on the
    night the clocks change is not local midnight; noon is always on the right
    date, in UTC as well as in Poland.
    """
    noon = datetime.fromtimestamp(service_day, UTC) + timedelta(hours=12)
    return noon.date().isoformat()
