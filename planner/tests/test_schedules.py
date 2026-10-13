"""Departures, stop timetables, lines and trips: OTP answers to ours, parameters to OTP."""

import copy
from datetime import UTC, date, datetime, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from planner import otp_runtime, schedules, vehicles
from planner.models import GraphBuild, PlannerSettings

pytestmark = pytest.mark.django_db

V1 = '/api/planner/v1/'
# Service day of 2026-10-14 in Poland: local midnight (CEST), 2026-10-13T22:00:00Z.
DAY = 1791928800


def otp_route(route_id='krk:70', short='70', mode='TRAM'):
    return {
        'gtfsId': route_id,
        'shortName': short,
        'longName': None,
        'mode': mode,
        'color': 'FF0000',
        'textColor': None,
        'agency': {'gtfsId': 'krk:302', 'name': 'MPK Kraków'},
    }


def stoptime(trip, seconds, *, route=None, delay=None, state='SCHEDULED', headsign='Pleszów'):
    realtime = delay is not None
    return {
        'scheduledDeparture': seconds,
        'realtimeDeparture': seconds + (delay or 0),
        'departureDelay': delay or 0,
        'realtime': realtime,
        'realtimeState': state,
        'serviceDay': DAY,
        'headsign': headsign,
        'stop': {'platformCode': None},
        'trip': {'gtfsId': trip, 'tripHeadsign': 'Salwator', 'route': route or otp_route()},
    }


LINE_1 = otp_route('krk:1', '1')
POLE_1 = [
    stoptime('krk:t1', 28800, delay=120, state='UPDATED'),
    stoptime('krk:t3', 29400),
]
POLE_2 = [
    stoptime('krk:t2', 28860, route=LINE_1, headsign='Salwator'),
    stoptime('krk:t4', 29000, route=LINE_1, delay=0, state='CANCELED', headsign=None),
]
# Next departures per stop (no line filter).
DEPARTURES = {
    'stops': [
        {'gtfsId': 'krk:1', 'stoptimesWithoutPatterns': POLE_1},
        {'gtfsId': 'krk:2', 'stoptimesWithoutPatterns': POLE_2},
        None,  # an unknown ID next to known ones
    ]
}
# Per line variant (with a line filter).
DEPARTURES_BY_VARIANT = {
    'stops': [
        {'gtfsId': 'krk:1', 'stoptimesForPatterns': [{'stoptimes': POLE_1}]},
        {'gtfsId': 'krk:2', 'stoptimesForPatterns': [{'stoptimes': POLE_2}]},
    ]
}
# 2026-10-12 to 2026-12-13 in Poland, as OTP reports it.
SERVICE_TIME_RANGE = {'start': 1791756000, 'end': 1797202800}


@pytest.fixture(autouse=True)
def enabled():
    PlannerSettings.objects.update_or_create(pk=1, defaults={'public_api_enabled': True})


@pytest.fixture
def otp(monkeypatch):
    class Fake:
        answer = {}
        answers = []  # answered in order before falling back to `answer`
        calls = []

        def query(self, document, variables=None, timeout=None):
            self.calls.append({'document': document, 'variables': variables})
            return copy.deepcopy(self.answers.pop(0) if self.answers else self.answer)

        @property
        def variables(self):
            return self.calls[-1]['variables']

    fake = Fake()
    fake.calls = []
    fake.answers = []
    monkeypatch.setattr(otp_runtime, 'query', fake.query)
    return fake


def get(path, params=None):
    return APIClient().get(f'{V1}{path}', params or {}, REMOTE_ADDR='198.51.100.7')


# ---------------------------------------------------------------------------
# Departures
# ---------------------------------------------------------------------------


def test_departures_of_several_poles_soonest_first(otp):
    otp.answer = DEPARTURES

    body = get(
        'stop/departures/',
        {'id': ['krk:1', 'krk:2'], 'time': '2026-10-14T08:00:00+02:00', 'limit': 3},
    ).json()

    assert otp.calls[0]['document'] == schedules.DEPARTURES_DOCUMENT
    assert otp.variables == {
        'ids': ['krk:1', 'krk:2'],
        'start': int(datetime(2026, 10, 14, 6, tzinfo=UTC).timestamp()),
        'range': 3600,
        'count': 3,
        'byVariant': False,
    }
    # Ordered by the predicted time when there is one: t2 (08:01) before t1 (08:00 + 2 min).
    assert [row['trip_id'] for row in body['departures']] == ['krk:t2', 'krk:t1', 'krk:t4']
    assert body['departures'][1] == {
        'stop_id': 'krk:1',
        'platform': None,
        'route': {
            'id': 'krk:70',
            'short_name': '70',
            'long_name': None,
            'mode': 'tram',
            'color': 'FF0000',
            'text_color': None,
            'agency': {'id': 'krk:302', 'name': 'MPK Kraków'},
        },
        'trip_id': 'krk:t1',
        'service_date': '2026-10-14',
        'headsign': 'Pleszów',
        'scheduled': '2026-10-14T06:00:00Z',
        'estimated': '2026-10-14T06:02:00Z',
        'delay_s': 120,
        'realtime': True,
        'status': 'updated',
    }
    cancelled = body['departures'][2]
    assert (cancelled['status'], cancelled['headsign']) == ('canceled', 'Salwator')


def test_departures_of_chosen_lines_from_now(otp):
    otp.answer = DEPARTURES_BY_VARIANT

    body = get('stop/departures/', {'id': 'krk:1', 'routes': 'krk:1'}).json()

    # Per variant, so other lines cannot take every place.
    assert otp.variables['byVariant'] is True
    assert [row['trip_id'] for row in body['departures']] == ['krk:t2', 'krk:t4']
    assert abs(otp.variables['start'] - timezone.now().timestamp()) < 5


def test_departures_of_unknown_stops_are_404(otp):
    otp.answer = {'stops': [None]}

    response = get('stop/departures/', {'id': 'krk:nope'})

    assert (response.status_code, response.json()['code']) == (404, 'planner.stop_not_found')


@pytest.mark.parametrize(
    ('params', 'fields'),
    [
        ({}, {'id': ['required']}),
        ({'id': [f'krk:{n}' for n in range(21)]}, {'id': ['max_length']}),
        ({'id': 'krk:1', 'window': '721'}, {'window': ['max_value']}),
        ({'id': 'krk:1', 'time': '2026-10-14T08:00'}, {'time': ['planner.time_without_offset']}),
    ],
)
def test_departure_parameter_errors(otp, params, fields):
    response = get('stop/departures/', params)

    assert response.json() == {'code': 'planner.invalid_request', 'details': {'fields': fields}}
    assert otp.calls == []


# ---------------------------------------------------------------------------
# Stop timetable
# ---------------------------------------------------------------------------


def test_stop_timetable_by_line_and_headsign(otp):
    def group(route, headsign, *seconds):
        return {
            'pattern': {'headsign': headsign, 'route': route},
            'stoptimes': [
                {
                    'scheduledDeparture': s,
                    'serviceDay': DAY,
                    'realtimeState': 'SCHEDULED',
                    'trip': {'gtfsId': f'krk:{s}'},
                }
                for s in seconds
            ],
        }

    otp.answer = {
        'serviceTimeRange': SERVICE_TIME_RANGE,
        'stop': {
            'gtfsId': 'krk:1',
            'name': 'Starowiślna',
            'platformCode': None,
            'stoptimesForServiceDate': [
                group(otp_route('krk:10', '10'), 'Łagiewniki', 30000),
                # Two variants with the same headsign are one line.
                group(otp_route(), 'Pleszów', 29400),
                group(otp_route(), 'Pleszów', 28800),
                group(otp_route('krk:2', '2'), 'Cmentarz', 90000),  # after midnight
            ],
        },
    }

    body = get('stop/timetable/', {'id': 'krk:1', 'date': '2026-10-14'}).json()

    assert otp.variables == {'id': 'krk:1', 'date': '20261014'}
    assert body['stop'] == {'id': 'krk:1', 'name': 'Starowiślna', 'platform': None}
    assert body['date'] == '2026-10-14'
    assert [(line['route']['short_name'], line['headsign']) for line in body['lines']] == [
        ('2', 'Cmentarz'),
        ('10', 'Łagiewniki'),
        ('70', 'Pleszów'),
    ]
    assert body['lines'][2]['departures'] == [
        {'time': '2026-10-14T06:00:00Z', 'trip_id': 'krk:28800', 'canceled': False},
        {'time': '2026-10-14T06:10:00Z', 'trip_id': 'krk:29400', 'canceled': False},
    ]
    # 25:00:00 on the 14th's service day is 01:00 on the 15th.
    assert body['lines'][0]['departures'][0]['time'] == '2026-10-14T23:00:00Z'


def test_stop_timetable_defaults_to_today_in_poland(otp, monkeypatch):
    otp.answer = {'stop': {'gtfsId': 'krk:1', 'name': 'X', 'stoptimesForServiceDate': []}}
    monkeypatch.setattr(schedules, 'today', lambda: date(2026, 10, 25))

    assert get('stop/timetable/', {'id': 'krk:1'}).json()['date'] == '2026-10-25'
    assert otp.variables['date'] == '20261025'


def test_today_is_the_polish_calendar_day(monkeypatch):
    # 23:30 UTC on the 13th is already the 14th in Warsaw.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, 23, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(schedules, 'datetime', Clock)

    assert schedules.today() == date(2026, 10, 14)


# ---------------------------------------------------------------------------
# Line and trip
# ---------------------------------------------------------------------------


def test_line_variants_main_first_and_paths_on_request(otp):
    def pattern(direction, headsign, stops, points='abc'):
        return {
            'headsign': headsign,
            'directionId': direction,
            'stops': [
                {
                    'gtfsId': f'krk:{n}',
                    'name': f'S{n}',
                    'lat': 50.0,
                    'lon': 19.9,
                    'platformCode': None,
                }
                for n in range(stops)
            ],
            'patternGeometry': {'points': points},
        }

    otp.answer = {
        'route': {
            **otp_route(),
            'patterns': [
                pattern(1, 'Bardosa', 2),
                pattern(0, 'Kurdwanów', 3),
                pattern(1, 'Pleszów', 4),
                pattern(-1, 'Zajezdnia', 1),  # OTP: direction irrelevant
            ],
        }
    }

    body = get('route/', {'id': 'krk:70', 'geometry': 'true'}).json()

    assert otp.variables == {'id': 'krk:70', 'withGeometry': True}
    assert [(v['direction_id'], v['headsign'], len(v['stops'])) for v in body['variants']] == [
        (0, 'Kurdwanów', 3),
        (1, 'Pleszów', 4),
        (1, 'Bardosa', 2),
        (None, 'Zajezdnia', 1),
    ]
    assert body['variants'][0]['geometry'] == 'abc'
    assert body['short_name'] == '70'


def test_unknown_line_is_404(otp):
    otp.answer = {'route': None}

    assert get('route/', {'id': 'krk:nope'}).json()['code'] == 'planner.route_not_found'


TRIP = {
    'serviceTimeRange': SERVICE_TIME_RANGE,
    'trip': {
        'gtfsId': 'krk:t1',
        'tripHeadsign': 'Pleszów',
        'directionId': '1',
        'blockId': 'T/70-08',
        'route': otp_route(),
        'onServiceDate': {
            'serviceDate': '2026-10-14',
            'stopCalls': [
                {
                    'stopLocation': {
                        'gtfsId': 'krk:1',
                        'name': 'Kurdwanów P+R',
                        'lat': 50.01,
                        'lon': 19.94,
                        'platformCode': '2',
                    },
                    'schedule': {
                        'time': {
                            'arrival': '2026-10-14T07:55:00+02:00',
                            'departure': '2026-10-14T07:55:00+02:00',
                        }
                    },
                    'realTime': {
                        'arrival': {'time': '2026-10-14T07:56:00+02:00', 'delay': 'PT1M'},
                        'departure': {'time': '2026-10-14T07:56:30+02:00', 'delay': 'PT1M30S'},
                    },
                },
                {
                    'stopLocation': {
                        'gtfsId': 'krk:2',
                        'name': 'Pleszów',
                        'lat': 50.07,
                        'lon': 20.11,
                        'platformCode': None,
                    },
                    'schedule': {
                        'time': {
                            'arrival': '2026-10-14T08:53:00+02:00',
                            'departure': '2026-10-14T08:53:00+02:00',
                        }
                    },
                    'realTime': None,
                },
            ],
        },
    },
}


def test_trip_on_its_service_date_with_realtime(otp):
    otp.answer = TRIP

    body = get('trip/', {'id': 'krk:t1', 'date': '2026-10-14'}).json()

    assert otp.variables == {'id': 'krk:t1', 'date': '2026-10-14', 'withGeometry': False}
    assert body | {'calls': None, 'route': None} == {
        'id': 'krk:t1',
        'headsign': 'Pleszów',
        'direction_id': 1,
        'block_id': 'T/70-08',
        'route': None,
        'service_date': '2026-10-14',
        'realtime': True,
        'calls': None,
        'geometry': None,
    }
    assert body['calls'][0] == {
        'stop': {
            'id': 'krk:1',
            'name': 'Kurdwanów P+R',
            'lat': 50.01,
            'lon': 19.94,
            'platform': '2',
        },
        'arrival': {
            'scheduled': '2026-10-14T05:55:00Z',
            'estimated': '2026-10-14T05:56:00Z',
            'delay_s': 60,
        },
        'departure': {
            'scheduled': '2026-10-14T05:55:00Z',
            'estimated': '2026-10-14T05:56:30Z',
            'delay_s': 90,
        },
    }
    assert body['calls'][1]['arrival']['estimated'] is None


@pytest.mark.parametrize(
    ('answer', 'day', 'code'),
    [
        ({'trip': None}, '2026-10-20', 'planner.trip_not_found'),
        (
            {**TRIP, 'trip': {**TRIP['trip'], 'onServiceDate': None}},
            '2026-10-18',
            'planner.trip_not_on_date',
        ),
        (
            {**TRIP, 'trip': {**TRIP['trip'], 'onServiceDate': None}},
            '2026-12-14',
            'planner.date_outside_service_period',
        ),
    ],
)
def test_missing_trip_or_day_is_404(otp, answer, day, code):
    otp.answer = answer

    response = get('trip/', {'id': 'krk:t1', 'date': day})

    assert (response.status_code, response.json()['code']) == (404, code)


def test_timetable_outside_the_data_says_which_dates_exist(otp):
    otp.answer = {
        'serviceTimeRange': SERVICE_TIME_RANGE,
        'stop': {'gtfsId': 'krk:1', 'name': 'X', 'stoptimesForServiceDate': []},
    }

    inside = get('stop/timetable/', {'id': 'krk:1', 'date': '2026-12-13'})
    outside = get('stop/timetable/', {'id': 'krk:1', 'date': '2026-10-11'})

    assert inside.status_code == 200
    assert (outside.status_code, outside.json()) == (
        404,
        {
            'code': 'planner.date_outside_service_period',
            'details': {'first_date': '2026-10-12', 'last_date': '2026-12-13'},
        },
    )


# ---------------------------------------------------------------------------
# Time and availability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('service_day', 'seconds', 'expected_instant', 'expected_date'),
    [
        # 2026-10-25, clocks go back: the service day starts 23:00 UTC (01:00 CEST),
        # so 02:30 GTFS time is 01:30 UTC, i.e. 02:30 CET (the second 02:30).
        (
            int(datetime(2026, 10, 24, 23, tzinfo=UTC).timestamp()),
            9000,
            '2026-10-25T01:30:00Z',
            '2026-10-25',
        ),
        # 2027-03-28, clocks go forward: the service day starts 22:00 UTC on the 27th
        # (23:00 CET), yet it is the 28th's service day.
        (
            int(datetime(2027, 3, 27, 22, tzinfo=UTC).timestamp()),
            36000,
            '2027-03-28T08:00:00Z',
            '2027-03-28',
        ),
    ],
)
def test_service_days_across_the_dst_changes(service_day, seconds, expected_instant, expected_date):
    assert schedules.instant(service_day, seconds) == expected_instant
    assert schedules.service_date(service_day) == expected_date


def test_schedules_are_unavailable_while_otp_is_stopped(otp):
    GraphBuild.objects.create(
        trigger='manual',
        otp_image='x',
        status=GraphBuild.STATUS_RUNNING,
        otp_stopped_at=timezone.now(),
    )

    response = get('stop/departures/', {'id': 'krk:1'})

    assert (response.status_code, response.json()['details']) == (503, {'reason': 'maintenance'})
    assert otp.calls == []


# ---------------------------------------------------------------------------
# Blocks and vehicles
# ---------------------------------------------------------------------------


def block_trip(trip_id, start, end, *, delay=None):
    def call(stop, time, which):
        return {
            'stopLocation': {'gtfsId': stop, 'name': stop},
            'schedule': {'time': {which: time}},
            'realTime': {which: {'time': time, 'delay': delay}} if delay else None,
        }

    return {
        'gtfsId': trip_id,
        'tripHeadsign': 'Pleszów',
        'route': otp_route(),
        'onServiceDate': {
            'start': call('krk:a', start, 'departure'),
            'end': call('krk:b', end, 'arrival'),
        },
    }


@pytest.fixture
def block():
    from planner.models import IndexedBlockTrip

    for trip_id in ('krk:t1', 'krk:t2', 'krk:t3'):
        IndexedBlockTrip.objects.create(feed_id='krk', block_id='T/70-08', trip_id=trip_id)


def test_block_lists_the_days_trips_in_order_and_marks_the_running_one(otp, block, monkeypatch):
    monkeypatch.setattr(timezone, 'now', lambda: datetime(2026, 10, 14, 6, 30, tzinfo=UTC))
    otp.answer = {
        'serviceTimeRange': SERVICE_TIME_RANGE,
        # Ordered by trip ID in the index, so t0 (krk:t1) runs later than t1 (krk:t2).
        't0': block_trip('krk:t1', '2026-10-14T09:00:00+02:00', '2026-10-14T10:00:00+02:00'),
        't1': block_trip(
            'krk:t2', '2026-10-14T08:00:00+02:00', '2026-10-14T08:50:00+02:00', delay='PT2M'
        ),
        't2': {**block_trip('krk:t3', 'x', 'y'), 'onServiceDate': None},  # another day type
    }

    body = get('trip/block/', {'id': 'krk:t2', 'date': '2026-10-14'}).json()

    assert otp.variables == {
        'date': '2026-10-14',
        't0': 'krk:t1',
        't1': 'krk:t2',
        't2': 'krk:t3',
    }
    assert (body['feed_id'], body['block_id'], body['service_date']) == (
        'krk',
        'T/70-08',
        '2026-10-14',
    )
    assert [(t['id'], t['current'], t['realtime']) for t in body['trips']] == [
        ('krk:t2', True, True),
        ('krk:t1', False, False),
    ]
    assert body['trips'][0]['start'] == {
        'stop': {'id': 'krk:a', 'name': 'krk:a', 'lat': None, 'lon': None, 'platform': None},
        'scheduled': '2026-10-14T06:00:00Z',
        'estimated': '2026-10-14T06:00:00Z',
        'delay_s': 120,
    }


def test_block_of_a_trip_without_one_is_404(otp):
    response = get('trip/block/', {'id': 'krk:t9', 'date': '2026-10-14'})

    assert (response.status_code, response.json()) == (
        404,
        {'code': 'planner.block_not_available', 'details': {'reason': 'no_block'}},
    )
    assert otp.calls == []


def test_trip_after_midnight_defaults_to_yesterdays_service(otp, monkeypatch):
    monkeypatch.setattr(schedules, 'today', lambda: date(2026, 10, 14))
    monkeypatch.setattr(timezone, 'now', lambda: datetime(2026, 10, 13, 22, 30, tzinfo=UTC))
    # Yesterday's run ends at 00:50 local (22:50 UTC), after "now".
    yesterday_run = {
        'trip': {
            'onServiceDate': {
                'end': {
                    'schedule': {'time': {'arrival': '2026-10-14T00:50:00+02:00'}},
                    'realTime': None,
                }
            }
        }
    }
    otp.answers = [yesterday_run, TRIP]

    body = get('trip/', {'id': 'krk:t1'}).json()

    assert otp.calls[0]['variables'] == {'id': 'krk:t1', 'yesterday': '2026-10-13'}
    assert otp.calls[1]['variables']['date'] == '2026-10-13'
    assert body['id'] == 'krk:t1'


def test_trip_defaults_to_today_once_yesterdays_run_ended(otp, monkeypatch):
    monkeypatch.setattr(schedules, 'today', lambda: date(2026, 10, 14))
    now = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)

    ended = {
        'trip': {
            'onServiceDate': {
                'end': {'schedule': {'time': {'arrival': '2026-10-13T23:50:00+02:00'}}}
            }
        }
    }

    assert schedules.current_service_date(ended, now) == date(2026, 10, 14)
    assert schedules.current_service_date({'trip': None}, now) == date(2026, 10, 14)


def otp_vehicle(vehicle_id, lat, lon, *, minutes_ago=0, trip='krk:t1', status='STOPPED_AT'):
    updated = timezone.now() - timedelta(minutes=minutes_ago)
    return {
        'vehicleId': vehicle_id,
        'label': vehicle_id.rsplit(':', 1)[-1],
        'lat': lat,
        'lon': lon,
        'heading': 90.0,
        'speed': 5.5,
        'lastUpdate': updated.isoformat(),
        'stopRelationship': {'status': status, 'stop': {'gtfsId': 'krk:1', 'name': 'Starowiślna'}},
        'trip': {'gtfsId': trip, 'tripHeadsign': 'Pleszów', 'blockId': 'T/70-08'},
    }


VEHICLES = {
    'routes': [
        {
            **otp_route(),
            'patterns': [
                {
                    'headsign': 'Pleszów',
                    'vehiclePositions': [
                        otp_vehicle('krk:T:2', 50.06, 19.94),
                        otp_vehicle('krk:T:9', 50.06, 19.94, minutes_ago=10),  # stopped reporting
                    ],
                },
                {'headsign': 'Bardosa', 'vehiclePositions': []},
            ],
        },
        {
            **otp_route('krk:152', '152', 'BUS'),
            'patterns': [
                {'headsign': 'X', 'vehiclePositions': [otp_vehicle('krk:A:1', 50.5, 19.0)]}
            ],
        },
    ]
}


def test_vehicles_now_with_their_trip_and_block(otp):
    otp.answer = VEHICLES

    body = get('vehicles/').json()

    assert body['count'] == 2  # the stale position is left out
    tram = body['results'][1]
    assert tram | {'updated_at': None} == {
        'id': 'krk:T:2',
        'label': '2',
        'lat': 50.06,
        'lon': 19.94,
        'heading': 90.0,
        'speed_m_s': 5.5,
        'updated_at': None,
        'route': {
            'id': 'krk:70',
            'short_name': '70',
            'long_name': None,
            'mode': 'tram',
            'color': 'FF0000',
            'text_color': None,
            'agency': {'id': 'krk:302', 'name': 'MPK Kraków'},
        },
        'headsign': 'Pleszów',
        'trip_id': 'krk:t1',
        'block_id': 'T/70-08',
        'stop_status': 'stopped_at',
        'stop': {'id': 'krk:1', 'name': 'Starowiślna'},
    }
    assert tram['updated_at'].endswith('Z')


def test_vehicles_are_filtered_from_one_shared_snapshot(otp):
    otp.answer = VEHICLES

    def ids(params):
        return [row['id'] for row in get('vehicles/', params).json()['results']]

    assert ids({'bbox': '19.9,50.0,20.0,50.1'}) == ['krk:T:2']
    assert ids({'modes': 'bus'}) == ['krk:A:1']
    assert ids({'routes': 'krk:70'}) == ['krk:T:2']
    assert ids({'agencies': 'krk:302'}) == ['krk:A:1', 'krk:T:2']
    assert len(otp.calls) == 1  # one OTP query for all of them


def test_vehicles_wait_briefly_for_a_refresh_then_get_busy(otp, monkeypatch):
    monkeypatch.setattr(vehicles, 'REFRESH_WAIT_SECONDS', 0.01)
    vehicles._lock.acquire()  # another request is refreshing
    try:
        response = get('vehicles/')
    finally:
        vehicles._lock.release()

    assert (response.status_code, response.json()['code']) == (503, 'planner.busy')
    assert response['Retry-After'] == '5'
    assert otp.calls == []


def test_vehicles_get_the_previous_snapshot_while_another_request_refreshes(otp, monkeypatch):
    otp.answer = VEHICLES
    assert get('vehicles/').json()['count'] == 2
    monkeypatch.setattr(vehicles, 'REFRESH_WAIT_SECONDS', 0.01)
    vehicles._snapshot['until'] = 0.0  # expired
    vehicles._lock.acquire()
    try:
        assert get('vehicles/').json()['count'] == 2
    finally:
        vehicles._lock.release()
    assert len(otp.calls) == 1


def test_a_vehicle_without_id_is_listed_last_not_a_crash(otp):
    answer = copy.deepcopy(VEHICLES)
    answer['routes'][1]['patterns'][0]['vehiclePositions'][0]['vehicleId'] = None
    otp.answer = answer

    assert [row['id'] for row in get('vehicles/').json()['results']] == [None, 'krk:T:2']
