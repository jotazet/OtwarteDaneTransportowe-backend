"""Trip planning: our /plan/ parameters to OTP's planConnection, and its answer to our shape.

The GraphQL document is fixed; client input only ever travels as variables.
Field names follow OTP 2.10's GTFS GraphQL schema (planConnection).
"""

import re
from datetime import UTC, datetime

from django.utils.dateparse import parse_duration
from rest_framework.exceptions import ErrorDetail

# A stop call's times, as call_times() reads them; append CALL_TIMES_FRAGMENT to the document.
CALL_TIMES_FRAGMENT = """
fragment CallTimes on StopCall {
  schedule { time { ... on ArrivalDepartureTime { arrival departure } } }
  realTime { arrival { time delay } departure { time delay } }
}
"""

PLAN_DOCUMENT = (
    """
query Plan(
  $origin: PlanLabeledLocationInput!
  $destination: PlanLabeledLocationInput!
  $dateTime: PlanDateTimeInput
  $modes: PlanModesInput
  $preferences: PlanPreferencesInput
  $via: [PlanViaLocationInput!]
  $first: Int
  $after: String
  $last: Int
  $before: String
  $withStops: Boolean!
  $withGeometry: Boolean!
) {
  planConnection(
    origin: $origin
    destination: $destination
    dateTime: $dateTime
    modes: $modes
    preferences: $preferences
    via: $via
    first: $first
    after: $after
    last: $last
    before: $before
  ) {
    routingErrors { code }
    pageInfo { startCursor endCursor }
    edges {
      node {
        start
        end
        duration
        walkDistance
        numberOfTransfers
        legs {
          mode
          duration
          distance
          headsign
          serviceDate
          start { scheduledTime estimated { time delay } }
          end { scheduledTime estimated { time delay } }
          rentedBike
          from {
            name lat lon stop { gtfsId platformCode }
            vehicleRentalStation { stationId } rentalVehicle { vehicleId }
          }
          to {
            name lat lon stop { gtfsId platformCode }
            vehicleRentalStation { stationId } rentalVehicle { vehicleId }
          }
          route { gtfsId shortName longName color textColor }
          agency { gtfsId name }
          trip { gtfsId }
          legGeometry @include(if: $withGeometry) { points }
          alerts {
            id
            alertHeaderText
            alertDescriptionText
            alertUrl
            alertEffect
            alertSeverityLevel
          }
          stopCalls @include(if: $withStops) {
            stopLocation { ... on Stop { gtfsId name lat lon } }
            ...CallTimes
          }
        }
      }
    }
  }
}
"""
    + CALL_TIMES_FRAGMENT
)

# Our transit mode names (lowercase) to OTP's TransitMode.
TRANSIT_MODES = {
    'bus': 'BUS',
    'coach': 'COACH',
    'tram': 'TRAM',
    'trolleybus': 'TROLLEYBUS',
    'subway': 'SUBWAY',
    'rail': 'RAIL',
    'ferry': 'FERRY',
    'funicular': 'FUNICULAR',
    'cable_car': 'CABLE_CAR',
    'gondola': 'GONDOLA',
    'monorail': 'MONORAIL',
}
# Shared vehicles (GBFS) the trip may use to reach, leave or replace transit.
RENTAL_MODES = {'bicycle': 'BICYCLE_RENTAL', 'scooter': 'SCOOTER_RENTAL'}
# m/s. Presets rather than any number: OTP caches transfers per set of walking
# preferences, so a few fixed values keep that cache (and memory) bounded.
WALK_SPEEDS = {'slow': 1.0, 'normal': 1.33, 'fast': 1.7}
COORDINATE = re.compile(r'^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$')
# OTP's feed-scoped IDs: `<feed_id>:<id in the feed>`, both parts non-empty.
FEED_SCOPED_ID = re.compile(r'^[^:]+:.+$')
CURSOR_NEXT = 'n:'
CURSOR_PREVIOUS = 'p:'
# OTP 2.10's messages for well-formed input it cannot use (GraphQL errors, not
# routing errors): (text in the message, parameter, code).
REJECTED_INPUTS = (
    ('Unable to decode page cursor', 'cursor', 'planner.invalid_cursor'),
    ('entity not found', 'via', 'planner.stop_not_found'),
)


def parse_place(value: str) -> dict:
    """``lat,lon`` or a feed-prefixed stop/station ID, as OTP's PlanLocationInput."""
    match = COORDINATE.match(value)
    if match:
        return {'coordinate': {'latitude': float(match[1]), 'longitude': float(match[2])}}
    return {'stopLocation': {'stopLocationId': value.strip()}}


def plan_variables(params: dict) -> dict:
    """GraphQL variables for validated /plan/ parameters (see PlanQuerySerializer)."""
    preferences = {
        'accessibility': {'wheelchair': {'enabled': params['wheelchair']}},
        'street': {'walk': {'speed': WALK_SPEEDS[params['walk_speed']]}},
        'transit': {
            'timetable': {'excludeRealTimeUpdates': not params['realtime']},
            'transfer': {'slack': f"PT{params['min_transfer_minutes']}M"},
        },
    }
    if params.get('max_transfers') is not None:
        preferences['transit']['transfer']['maximumTransfers'] = params['max_transfers']
    filters = _filters(params)
    if filters:
        preferences['transit']['filters'] = filters

    variables = {
        'origin': {'location': parse_place(params['from'])},
        'destination': {'location': parse_place(params['to'])},
        'preferences': preferences,
        'withStops': params['stops'],
        'withGeometry': params['geometry'],
    }
    if params.get('time'):
        key = 'latestArrival' if params['arrive_by'] else 'earliestDeparture'
        variables['dateTime'] = {key: params['time'].isoformat()}
    modes = {}
    if params.get('modes'):
        modes['transit'] = {'transit': [{'mode': TRANSIT_MODES[m]} for m in params['modes']]}
    if params.get('rental'):
        # With WALK, renting is allowed, not required (OTP's PlanModesInput).
        street = ['WALK', *(RENTAL_MODES[kind] for kind in params['rental'])]
        modes['direct'] = street
        modes.setdefault('transit', {}).update(access=street, egress=street)
    if modes:  # never null: OTP 2.10 fails on an explicit null
        variables['modes'] = modes
    if params.get('via'):
        variables['via'] = [{'visit': {'stopLocationIds': [stop]}} for stop in params['via']]

    cursor = params.get('cursor') or ''
    if cursor.startswith(CURSOR_PREVIOUS):
        variables.update(last=params['num_itineraries'], before=cursor[len(CURSOR_PREVIOUS) :])
    else:
        variables['first'] = params['num_itineraries']
        if cursor.startswith(CURSOR_NEXT):
            variables['after'] = cursor[len(CURSOR_NEXT) :]
    return variables


def _filters(params: dict) -> list[dict]:
    """OTP transit filters: include lists narrow the search, exclude lists win over them."""
    select = {}
    for key in ('agencies', 'routes'):
        if params.get(key):
            select.setdefault('include', []).append({key: params[key]})
        if params.get(f'exclude_{key}'):
            select.setdefault('exclude', []).append({key: params[f'exclude_{key}']})
    return [select] if select else []


def input_errors(messages: list[str]) -> dict:
    """Field errors for OTP's GraphQL errors that the plan parameters caused."""
    return {
        field: [ErrorDetail('Rejected by the routing engine.', code=code)]
        for text, field, code in REJECTED_INPUTS
        if any(text in message for message in messages)
    }


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


def plan_response(data: dict) -> dict:
    connection = data['planConnection']
    page = connection.get('pageInfo') or {}
    return {
        'itineraries': [_itinerary(edge['node']) for edge in connection.get('edges') or []],
        'next_cursor': f"{CURSOR_NEXT}{page['endCursor']}" if page.get('endCursor') else None,
        'previous_cursor': (
            f"{CURSOR_PREVIOUS}{page['startCursor']}" if page.get('startCursor') else None
        ),
        'routing_errors': [
            {'code': f"planner.{error['code'].lower()}"}
            for error in connection.get('routingErrors') or []
        ],
    }


def _itinerary(node: dict) -> dict:
    legs = [_leg(leg) for leg in node['legs']]
    # OTP names the requested coordinates "Origin" and "Destination"; that is
    # its own English text, and the client knows what it asked for.
    for place in (legs[0]['from'], legs[-1]['to']) if legs else ():
        if place['stop_id'] is None:
            place['name'] = None
    return {
        'start': utc(node['start']),
        'end': utc(node['end']),
        'duration_s': node['duration'],
        'walk_distance_m': round(node.get('walkDistance') or 0),
        'transfers': node['numberOfTransfers'],
        'legs': legs,
    }


def _leg(leg: dict) -> dict:
    route = leg.get('route')
    agency = leg.get('agency')
    stop_calls = leg.get('stopCalls')
    return {
        'mode': _lower(leg['mode']),
        'from': _place(leg['from']),
        'to': _place(leg['to']),
        'start': _time(leg['start']),
        'end': _time(leg['end']),
        'duration_s': round(leg.get('duration') or 0),
        'distance_m': round(leg.get('distance') or 0),
        'route': (
            {
                'id': route['gtfsId'],
                'short_name': route.get('shortName'),
                'long_name': route.get('longName'),
                'color': route.get('color'),
                'text_color': route.get('textColor'),
            }
            if route
            else None
        ),
        'agency': {'id': agency['gtfsId'], 'name': agency['name']} if agency else None,
        'trip_id': (leg.get('trip') or {}).get('gtfsId'),
        'service_date': leg.get('serviceDate'),
        'headsign': leg.get('headsign'),
        'realtime': leg['start'].get('estimated') is not None,
        'rented': bool(leg.get('rentedBike')),
        'geometry': (leg.get('legGeometry') or {}).get('points'),
        'alerts': [_alert(alert) for alert in leg.get('alerts') or []],
        'stops': [_stop_call(call) for call in stop_calls] if stop_calls is not None else None,
    }


def _place(place: dict) -> dict:
    stop = place.get('stop') or {}
    return {
        'name': place.get('name'),
        'lat': place['lat'],
        'lon': place['lon'],
        'stop_id': stop.get('gtfsId'),
        'platform': stop.get('platformCode'),
        'rental_station_id': (place.get('vehicleRentalStation') or {}).get('stationId'),
        'rental_vehicle_id': (place.get('rentalVehicle') or {}).get('vehicleId'),
    }


def _time(leg_time: dict) -> dict:
    return call_time(leg_time['scheduledTime'], leg_time.get('estimated'))


def _alert(alert: dict) -> dict:
    return {
        'id': alert['id'],
        'header': alert.get('alertHeaderText'),
        'description': alert.get('alertDescriptionText'),
        'url': alert.get('alertUrl'),
        'effect': _lower(alert.get('alertEffect')),
        'severity': _lower(alert.get('alertSeverityLevel')),
    }


def _stop_call(call: dict) -> dict:
    location = call['stopLocation']
    return {
        'stop_id': location.get('gtfsId'),
        'name': location.get('name'),
        'lat': location.get('lat'),
        'lon': location.get('lon'),
        **call_times(call),
    }


def call_times(call: dict) -> dict:
    """A stop call's ``arrival`` and ``departure`` (selected with CALL_TIMES_FRAGMENT)."""
    # Flexible (GTFS-Flex) calls have a time window instead of times: `scheduled` is null.
    scheduled = (call.get('schedule') or {}).get('time') or {}
    realtime = call.get('realTime') or {}
    return {
        'arrival': call_time(scheduled.get('arrival'), realtime.get('arrival')),
        'departure': call_time(scheduled.get('departure'), realtime.get('departure')),
    }


def call_time(scheduled: str | None, estimate: dict | None) -> dict:
    """A stop call's arrival or departure: timetable time and realtime estimate, in UTC."""
    return {
        'scheduled': utc(scheduled),
        'estimated': utc(estimate['time']) if estimate else None,
        'delay_s': duration_seconds(estimate['delay']) if estimate else None,
    }


def _lower(value: str | None) -> str | None:
    """OTP's enum values (TRAM, DETOUR) in the lowercase the API uses."""
    return value.lower() if value else None


def utc(value: str | None) -> str | None:
    """OTP's offset date-times as UTC instants, like the rest of the API."""
    if value is None:
        return None
    moment = datetime.fromisoformat(value).astimezone(UTC)
    return moment.isoformat().replace('+00:00', 'Z')


def duration_seconds(value: str) -> int:
    """OTP's ISO-8601 durations, negative with a leading minus ("-PT1M30S"), in seconds."""
    return round(parse_duration(value).total_seconds())
