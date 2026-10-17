"""GET /api/planner/v1/plan/: parameters to OTP variables, OTP's answer to ours."""

import copy
import json
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from planner import otp_runtime, planning
from planner.models import IndexedAgency, PlannerSettings

pytestmark = pytest.mark.django_db

PLAN = '/api/planner/v1/plan/'
# A real OTP 2.10 answer (Kraków, tram 70), trimmed, with a realtime estimate and stop call times added.
OTP_ANSWER = json.loads((Path(__file__).parent / 'fixtures' / 'otp_plan_krakow.json').read_text())
EMPTY_ANSWER = {
    'planConnection': {'routingErrors': [], 'pageInfo': {}, 'edges': []},
}


@pytest.fixture(autouse=True)
def enabled():
    PlannerSettings.objects.update_or_create(pk=1, defaults={'public_api_enabled': True})


@pytest.fixture
def otp(monkeypatch):
    """Records the OTP queries and answers with ``otp.answer`` (or raises ``otp.error``)."""

    class Otp:
        answer = OTP_ANSWER
        error = None
        calls = []

        def query(self, document, variables=None, timeout=None):
            self.calls.append({'document': document, 'variables': variables, 'timeout': timeout})
            if self.error:
                raise self.error
            return copy.deepcopy(self.answer)

        @property
        def variables(self):
            return self.calls[-1]['variables']

    fake = Otp()
    fake.calls = []
    monkeypatch.setattr(otp_runtime, 'query', fake.query)
    return fake


def get(params):
    return APIClient().get(PLAN, params, REMOTE_ADDR='198.51.100.7')


def test_minimal_query_uses_defaults(otp):
    response = get({'from': '50.0617,19.9373', 'to': 'krk:2047513'})

    assert response.status_code == 200
    assert otp.calls[0]['document'] == planning.PLAN_DOCUMENT
    assert otp.variables == {
        'origin': {'location': {'coordinate': {'latitude': 50.0617, 'longitude': 19.9373}}},
        'destination': {'location': {'stopLocation': {'stopLocationId': 'krk:2047513'}}},
        'preferences': {
            'accessibility': {'wheelchair': {'enabled': False}},
            'street': {'walk': {'speed': 1.33}},
            'transit': {
                'timetable': {'excludeRealTimeUpdates': False},
                'transfer': {'slack': 'PT2M'},
            },
        },
        'withStops': False,
        'withGeometry': False,
        'first': 5,
    }


def test_every_parameter_reaches_otp_as_a_variable(otp):
    response = get(
        {
            'from': 'krk:2047470',
            'to': '50.07,20.03',
            'time': '2026-10-14T17:30:00+02:00',
            'arrive_by': 'true',
            'modes': ['tram', 'bus'],
            'wheelchair': 'true',
            'walk_speed': 'slow',
            'max_transfers': '1',
            'min_transfer_minutes': '4',
            'via': ['krk:2047482'],
            'agencies': ['krk:302'],
            'exclude_agencies': ['kml:1'],
            'routes': ['krk:352529'],
            'exclude_routes': ['krk:999'],
            'realtime': 'false',
            'num_itineraries': '3',
            'stops': 'true',
        }
    )

    assert response.status_code == 200
    variables = otp.variables
    assert variables['dateTime'] == {'latestArrival': '2026-10-14T15:30:00+00:00'}
    assert variables['modes'] == {'transit': {'transit': [{'mode': 'TRAM'}, {'mode': 'BUS'}]}}
    assert variables['via'] == [{'visit': {'stopLocationIds': ['krk:2047482']}}]
    assert variables['preferences'] == {
        'accessibility': {'wheelchair': {'enabled': True}},
        'street': {'walk': {'speed': 1.0}},
        'transit': {
            'timetable': {'excludeRealTimeUpdates': True},
            'transfer': {'slack': 'PT4M', 'maximumTransfers': 1},
            'filters': [
                {
                    'include': [{'agencies': ['krk:302']}, {'routes': ['krk:352529']}],
                    'exclude': [{'agencies': ['kml:1']}, {'routes': ['krk:999']}],
                }
            ],
        },
    }
    assert (variables['first'], variables['withStops']) == (3, True)
    assert variables['withGeometry'] is False
    assert otp.calls[0]['timeout'] == 12


def test_organizations_stand_for_all_their_operators(otp):
    mpk = TransportOrganization.objects.create(region='Kraków', transport_organization='ZTP')
    kml = TransportOrganization.objects.create(region='Małopolska', transport_organization='KML')
    for agency_id, org in (('krk:302', mpk), ('krk:303', mpk), ('kml:1', kml)):
        IndexedAgency.objects.create(agency_id=agency_id, feed_id='x', name='x', organization=org)

    response = get(
        {
            'from': '50.06,19.93',
            'to': '50.07,20.03',
            'exclude_organizations': [kml.id],
            'organizations': [mpk.id],
            'agencies': ['ext:9'],
        }
    )

    assert response.status_code == 200
    assert otp.variables['preferences']['transit']['filters'] == [
        {
            'include': [{'agencies': ['ext:9', 'krk:302', 'krk:303']}],
            'exclude': [{'agencies': ['kml:1']}],
        }
    ]


def test_organization_outside_the_planner_is_a_field_error(otp):
    response = get({'from': '50.06,19.93', 'to': '50.07,20.03', 'exclude_organizations': [42]})

    assert response.json()['details']['fields'] == {
        'exclude_organizations': ['planner.organization_not_found']
    }
    assert otp.calls == []


def test_geometry_only_on_request(otp):
    answer = copy.deepcopy(OTP_ANSWER)
    for leg in answer['planConnection']['edges'][0]['node']['legs']:
        del leg['legGeometry']  # OTP leaves it out unless withGeometry is true
    otp.answer = answer

    legs = get({'from': '50.06,19.93', 'to': '50.07,20.03'}).json()['itineraries'][0]['legs']
    assert [leg['geometry'] for leg in legs] == [None, None, None]

    otp.answer = OTP_ANSWER
    legs = get({'from': '50.06,19.93', 'to': '50.07,20.03', 'geometry': 'true'}).json()
    assert otp.variables['withGeometry'] is True
    assert legs['itineraries'][0]['legs'][1]['geometry'] == 'uxopH}uvxBA?'


@pytest.mark.parametrize(
    ('time', 'instant'),
    [
        # 2026-10-25: 02:00–03:00 happens twice in Poland, so the offset decides.
        ('2026-10-25T02:30:00+02:00', '2026-10-25T00:30:00+00:00'),
        ('2026-10-25T02:30:00+01:00', '2026-10-25T01:30:00+00:00'),
        ('2026-10-25T06:00:00Z', '2026-10-25T06:00:00+00:00'),
    ],
)
def test_time_is_an_instant_across_the_dst_change(otp, time, instant):
    get({'from': '50.06,19.93', 'to': '50.07,20.03', 'time': time})

    assert otp.variables['dateTime'] == {'earliestDeparture': instant}


def test_answer_is_mapped_to_utc_instants_ids_and_cursors(otp):
    body = get({'from': '50.0617,19.9373', 'to': '50.07,20.03', 'stops': 'true'}).json()

    assert (body['next_cursor'], body['previous_cursor']) == ('n:NEXT', 'p:PREV')
    assert body['routing_errors'] == []
    [itinerary] = body['itineraries']
    assert itinerary | {'legs': None} == {
        'start': '2026-10-14T06:03:30Z',
        'end': '2026-10-14T06:40:24Z',
        'duration_s': 2214,
        'walk_distance_m': 1178,
        'transfers': 0,
        'legs': None,
    }
    walk, tram, _ = itinerary['legs']
    # OTP's own labels for the requested points ("Origin") are not passed on.
    assert walk['from'] == {
        'name': None,
        'lat': 50.0617,
        'lon': 19.9373,
        'stop_id': None,
        'platform': None,
        'rental_station_id': None,
        'rental_vehicle_id': None,
    }
    assert walk['to']['stop_id'] == 'krk:2047470'
    assert (walk['mode'], walk['route'], walk['realtime'], walk['rented'], walk['stops']) == (
        'walk',
        None,
        False,
        False,
        [],
    )
    assert tram['mode'] == 'tram'
    assert tram['start'] == {
        'scheduled': '2026-10-14T06:15:00Z',
        'estimated': '2026-10-14T06:16:30Z',
        'delay_s': 90,
    }
    assert tram['end'] == {
        'scheduled': '2026-10-14T06:36:00Z',
        'estimated': '2026-10-14T06:35:00Z',
        'delay_s': -60,
    }
    assert tram['route'] == {
        'id': 'krk:352529',
        'short_name': '70',
        'long_name': None,
        'color': None,
        'text_color': None,
    }
    assert tram['agency'] == {'id': 'krk:302', 'name': 'Kraków'}
    assert (tram['trip_id'], tram['service_date'], tram['headsign'], tram['realtime']) == (
        'krk:53620411_256159',
        '2026-10-14',
        'Pleszów',
        True,
    )
    assert [stop['stop_id'] for stop in tram['stops']] == [
        'krk:2047470',
        'krk:2047482',
        'krk:2047513',
    ]
    assert tram['stops'][1] == {
        'stop_id': 'krk:2047482',
        'name': 'Hala Targowa',
        'lat': 50.058639,
        'lon': 19.948875,
        'arrival': {
            'scheduled': '2026-10-14T06:17:00Z',
            'estimated': '2026-10-14T06:18:00Z',
            'delay_s': 60,
        },
        'departure': {
            'scheduled': '2026-10-14T06:17:00Z',
            'estimated': '2026-10-14T06:18:00Z',
            'delay_s': 60,
        },
    }
    assert tram['alerts'][0] == {
        'id': 'QWxlcnQ6a3JrOjE',
        'header': 'Objazd',
        'description': None,
        'url': None,
        'effect': 'detour',
        'severity': 'warning',
    }


def test_stops_have_no_estimates_when_the_leg_has_none(otp):
    answer = copy.deepcopy(OTP_ANSWER)
    tram = answer['planConnection']['edges'][0]['node']['legs'][1]
    # With realtime=false OTP drops the leg's estimates but still fills its stop calls'.
    tram['start']['estimated'] = tram['end']['estimated'] = None
    otp.answer = answer

    body = get({'from': '50.06,19.93', 'to': '50.07,20.03', 'stops': 'true', 'realtime': 'false'})

    stops = body.json()['itineraries'][0]['legs'][1]['stops']
    assert {stop['departure']['estimated'] for stop in stops} == {None}
    assert {stop['arrival']['delay_s'] for stop in stops} == {None}
    assert stops[1]['departure']['scheduled'] == '2026-10-14T06:17:00Z'


def test_flexible_stop_has_no_scheduled_time(otp):
    answer = copy.deepcopy(OTP_ANSWER)
    call = answer['planConnection']['edges'][0]['node']['legs'][1]['stopCalls'][1]
    # A flexible call's schedule is a TimeWindow, which the ArrivalDepartureTime fragment skips.
    call['schedule'] = {'time': {}}
    call['realTime'] = None
    otp.answer = answer

    body = get({'from': '50.06,19.93', 'to': '50.07,20.03', 'stops': 'true'}).json()

    stop = body['itineraries'][0]['legs'][1]['stops'][1]
    empty = {'scheduled': None, 'estimated': None, 'delay_s': None}
    assert (stop['stop_id'], stop['arrival'], stop['departure']) == ('krk:2047482', empty, empty)


@pytest.mark.parametrize(
    ('cursor', 'paging'),
    [
        ('n:NEXT', {'first': 2, 'after': 'NEXT'}),
        ('p:PREV', {'last': 2, 'before': 'PREV'}),
    ],
)
def test_cursors_page_forwards_and_backwards(otp, cursor, paging):
    get({'from': '50.06,19.93', 'to': '50.07,20.03', 'num_itineraries': '2', 'cursor': cursor})

    assert {
        k: otp.variables[k] for k in ('first', 'after', 'last', 'before') if k in otp.variables
    } == paging


def test_routing_errors_are_codes_with_no_itineraries(otp):
    otp.answer = copy.deepcopy(EMPTY_ANSWER)
    otp.answer['planConnection']['routingErrors'] = [{'code': 'LOCATION_NOT_FOUND'}]

    response = get({'from': 'krk:nope', 'to': '50.07,20.03'})

    assert response.status_code == 200
    assert response.json() == {
        'itineraries': [],
        'next_cursor': None,
        'previous_cursor': None,
        'routing_errors': [{'code': 'planner.location_not_found'}],
    }


@pytest.mark.parametrize(
    ('params', 'fields'),
    [
        ({'to': '50.07,20.03'}, {'from': ['required']}),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'time': '2026-02-30T10:00:00+01:00'},
            {'time': ['invalid']},
        ),
        ({'from': 'nocolon', 'to': '50.07,20.03'}, {'from': ['planner.invalid_location']}),
        ({'from': '50.06,19.93', 'to': 'krk:'}, {'to': ['planner.invalid_location']}),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'agencies': ['krk:302', 'nocolon']},
            {'agencies': {'1': ['planner.invalid_id']}},
        ),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'arrive_by': 'true'},
            {'arrive_by': ['planner.arrive_by_without_time']},
        ),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'time': '2026-10-25T02:30:00'},
            {'time': ['planner.time_without_offset']},
        ),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'cursor': 'abc'},
            {'cursor': ['planner.invalid_cursor']},
        ),
        ({'from': '95.0,19.93', 'to': '50.07,20.03'}, {'from': ['planner.invalid_location']}),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'modes': ['plane']},
            {'modes': {'0': ['invalid_choice']}},
        ),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'via': ['a:1', 'a:2', 'a:3']},
            {'via': ['max_length']},
        ),
        (
            {'from': '50.06,19.93', 'to': '50.07,20.03', 'num_itineraries': '11'},
            {'num_itineraries': ['max_value']},
        ),
    ],
)
def test_invalid_parameters_return_field_codes_without_asking_otp(otp, params, fields):
    response = get(params)

    assert response.status_code == 400
    assert response.json() == {'code': 'planner.invalid_request', 'details': {'fields': fields}}
    assert otp.calls == []


@pytest.mark.parametrize(
    ('message', 'fields'),
    [
        (
            "Exception while fetching data (/planConnection) : Unable to decode page cursor: 'x'.",
            {'cursor': ['planner.invalid_cursor']},
        ),
        (
            'Exception while fetching data (/planConnection) : Stop, station, multimodal station'
            ' or group of stations entity not found: krk:missing',
            {'via': ['planner.stop_not_found']},
        ),
    ],
)
def test_input_otp_rejects_is_a_field_error(otp, message, fields):
    # Messages as OTP 2.10 words them.
    otp.error = otp_runtime.OtpQueryError([message])

    response = get({'from': '50.06,19.93', 'to': '50.07,20.03', 'via': ['krk:missing']})

    assert response.status_code == 400
    assert response.json() == {'code': 'planner.invalid_request', 'details': {'fields': fields}}


@pytest.mark.parametrize(
    ('error', 'status', 'body', 'retry_after'),
    [
        (otp_runtime.OtpBusy(), 503, {'code': 'planner.busy', 'details': {}}, '5'),
        (
            otp_runtime.OtpUnavailable('connection refused'),
            503,
            {'code': 'planner.unavailable', 'details': {'reason': 'engine'}},
            None,
        ),
        (
            otp_runtime.OtpQueryError(['Exception while fetching data: NullPointerException']),
            503,
            {'code': 'planner.unavailable', 'details': {'reason': 'engine'}},
            None,
        ),
    ],
)
def test_busy_or_silent_otp_is_a_503_code(otp, error, status, body, retry_after):
    otp.error = error

    response = get({'from': '50.06,19.93', 'to': '50.07,20.03'})

    assert (response.status_code, response.json()) == (status, body)
    assert response.headers.get('Retry-After') == retry_after


def test_plan_counts_against_the_plan_limit(otp):
    PlannerSettings.objects.filter(pk=1).update(anonymous_plan_per_minute=1)

    codes = [get({'from': '50.06,19.93', 'to': '50.07,20.03'}).status_code for _ in range(2)]

    assert codes == [200, 429]


@pytest.mark.parametrize(
    ('value', 'seconds'),
    [('PT1M30S', 90), ('-PT1M30S', -90), ('PT0S', 0), ('PT26H', 93600), ('PT0.6S', 1)],
)
def test_otp_durations_are_seconds(value, seconds):
    assert planning.duration_seconds(value) == seconds
