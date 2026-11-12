"""Position coverage: how many of an operator's trips under way report a vehicle position."""

import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from planner import otp_runtime, schedules, vehicles
from planner.models import GraphBuild, IndexedAgency, PlannerSettings

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)  # 14:00 in Kraków


def run(trip_id, start, end, *, delayed_end=None, canceled=False):
    """A TripOnServiceDate as OTP answers it; times are minutes from NOW."""

    def at(minutes):
        return (NOW + timedelta(minutes=minutes)).isoformat()

    return {
        'trip': {'gtfsId': trip_id},
        'realTimeTripState': {'canceled': canceled},
        'start': {'schedule': {'time': {'departure': at(start)}}, 'realTime': None},
        'end': {
            'schedule': {'time': {'arrival': at(end)}},
            'realTime': {'arrival': {'time': at(delayed_end)}} if delayed_end else None,
        },
    }


MPK = {
    'agency': {
        'gtfsId': 'krk:302',
        'name': 'MPK Kraków',
        'routes': [
            {
                'patterns': [
                    {
                        'today': [
                            run('krk:running', -10, 20),
                            run('krk:late', -60, -5, delayed_end=3),  # still on its way
                            run('krk:done', -60, -30),
                            run('krk:later', 10, 50),
                            run('krk:cancelled', -10, 20, canceled=True),
                        ],
                        # Yesterday's service day, past midnight, does not run at 14:00.
                        'yesterday': [run('krk:night', -13 * 60, -12 * 60)],
                    }
                ]
            }
        ],
    }
}


def vehicle(trip_id):
    return {
        'vehicleId': f'v-{trip_id}',
        'label': None,
        'lat': 50.06,
        'lon': 19.94,
        'heading': None,
        'speed': None,
        'lastUpdate': NOW.isoformat(),
        'stopRelationship': None,
        'trip': {'gtfsId': trip_id, 'tripHeadsign': None, 'blockId': None},
    }


VEHICLES = {
    'routes': [
        {
            'gtfsId': 'krk:70',
            'shortName': '70',
            'longName': None,
            'mode': 'TRAM',
            'color': None,
            'textColor': None,
            'agency': {'gtfsId': 'krk:302', 'name': 'MPK Kraków'},
            'patterns': [
                {
                    'headsign': None,
                    # One on a trip under way; one waiting at the depot for a later trip.
                    'vehiclePositions': [vehicle('krk:running'), vehicle('krk:later')],
                }
            ],
        }
    ]
}


@pytest.fixture
def graph():
    """The served graph; OTP polls vehicle positions for feed krk only."""
    return GraphBuild.objects.create(
        trigger='manual',
        otp_image='x',
        status=GraphBuild.STATUS_SUCCEEDED,
        is_active=True,
        feed_results=[
            {'feed_id': 'krk', 'outcome': 'fresh', 'realtime_polled': ['vehicle-positions']},
            {'feed_id': 'kml', 'outcome': 'stale', 'realtime_polled': []},
        ],
    )


@pytest.fixture
def otp(monkeypatch, graph):
    PlannerSettings.objects.update_or_create(pk=1, defaults={'public_api_enabled': True})
    monkeypatch.setattr(timezone, 'now', lambda: NOW)
    monkeypatch.setattr(schedules, 'today', lambda: date(2026, 10, 13))

    class Fake:
        calls = []
        running_error = None  # raised for the running-trips query when set

        def query(self, document, variables=None, timeout=None):
            self.calls.append(variables)
            if document == vehicles.VEHICLES_DOCUMENT:
                return VEHICLES
            if self.running_error:
                raise self.running_error
            if variables['agency'] == 'kml:1':
                return {'agency': {**MPK['agency'], 'gtfsId': 'kml:1', 'name': 'KML'}}
            return MPK if variables['agency'] == 'krk:302' else {'agency': None}

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(otp_runtime, 'query', fake.query)
    return fake


def running_queries(otp):
    return [call['agency'] for call in otp.calls if call and 'agency' in call]


def get(params):
    return APIClient().get('/api/planner/v1/vehicles/coverage/', params, REMOTE_ADDR='198.51.100.7')


def test_coverage_counts_trips_under_way_that_report_a_position(otp):
    body = get({'agencies': ['krk:302', 'krk:nope']}).json()

    assert body['results'] == [
        {
            'agency': {'id': 'krk:302', 'name': 'MPK Kraków'},
            'vehicle_positions': True,
            'trips_under_way': 2,  # running and late; not done, later, cancelled or night
            'trips_with_position': 1,  # the depot vehicle's trip has not started
            'coverage': 0.5,
        }
    ]
    assert otp.calls[1] == {'agency': 'krk:302', 'today': '2026-10-13', 'yesterday': '2026-10-12'}


def test_trips_under_way_are_reused_for_a_minute(otp):
    get({'agencies': 'krk:302'})
    get({'agencies': 'krk:302'})

    assert len(running_queries(otp)) == 1


def test_an_organization_stands_for_its_operators(otp):
    org = TransportOrganization.objects.create(region='Kraków', transport_organization='ZTP')
    IndexedAgency.objects.create(agency_id='krk:302', feed_id='krk', name='x', organization=org)

    body = get({'organizations': org.id}).json()

    assert [row['agency']['id'] for row in body['results']] == ['krk:302']


def test_no_trips_under_way_means_no_coverage_figure(otp, monkeypatch):
    monkeypatch.setattr(timezone, 'now', lambda: NOW + timedelta(hours=10))  # midnight

    [row] = get({'agencies': 'krk:302'}).json()['results']

    assert (row['trips_under_way'], row['coverage']) == (0, None)


def test_operators_must_be_named(otp):
    response = get({})

    assert response.json()['details']['fields'] == {'agencies': ['required']}
    assert otp.calls == []


def test_an_operator_without_position_data_has_no_coverage_figure(otp):
    # kml is served from a stale snapshot, so OTP polls no realtime for it.
    [row] = get({'agencies': 'kml:1'}).json()['results']

    assert (row['vehicle_positions'], row['trips_under_way'], row['coverage']) == (False, 2, None)


def test_while_one_request_recounts_others_use_the_previous_count(otp, graph):
    get({'agencies': 'krk:302'})
    key = f'planner:running:{graph.id}:krk:302'
    cache.delete(key)  # the minute is over
    cache.add(f'{key}:lock', 1, 60)  # another request is recounting

    [row] = get({'agencies': 'krk:302'}).json()['results']

    assert row['trips_under_way'] == 2
    assert len(running_queries(otp)) == 1


@pytest.fixture
def recounting(graph):
    """Starts a recount of MPK in another thread that holds until released."""
    started, release = threading.Event(), threading.Event()

    def slow_query(document, variables, **kwargs):
        started.set()
        release.wait(5)
        return MPK

    worker = threading.Thread(
        target=vehicles.running_trips, args=('krk:302', graph.id, slow_query, time.monotonic())
    )
    worker.start()
    assert started.wait(5)
    yield
    release.set()
    worker.join(5)


def test_concurrent_first_counts_run_one_recount_the_other_is_busy(otp, recounting):
    # Nothing counted yet (a new graph, or 10 idle minutes): the request that
    # does not recount has no previous count to answer with.
    response = get({'agencies': 'krk:302'})

    assert (response.status_code, response.json()) == (
        503,
        {'code': 'planner.busy', 'details': {}},
    )
    assert response.headers['Retry-After'] == '5'
    assert running_queries(otp) == []  # only the other request asked OTP


def test_a_process_recounts_one_operator_at_a_time(otp, recounting, monkeypatch):
    monkeypatch.setattr(vehicles, 'RECOUNT_WAIT_SECONDS', 0.01)

    response = get({'agencies': 'kml:1'})

    assert (response.status_code, response.json()['code']) == (503, 'planner.busy')
    assert running_queries(otp) == []  # kml:1 waited for the slot instead of querying OTP


def test_an_operator_waiting_for_the_slot_uses_its_previous_count(
    otp, graph, recounting, monkeypatch
):
    monkeypatch.setattr(vehicles, 'RECOUNT_WAIT_SECONDS', 0.01)
    key = f'planner:running:{graph.id}:kml:1'
    cache.set(f'{key}:previous', {'agency': {'id': 'kml:1', 'name': 'KML'}, 'trips': ['a']})

    [row] = get({'agencies': 'kml:1'}).json()['results']

    assert (row['agency']['id'], row['trips_under_way']) == ('kml:1', 1)
    assert running_queries(otp) == []
    assert cache.add(f'{key}:lock', 1)  # the losing request released the lock


def test_a_request_waits_for_the_slot_once_not_per_operator(otp, graph, monkeypatch):
    waits = []

    class TakenSlot:
        def acquire(self, timeout):
            waits.append(timeout)
            time.sleep(timeout)
            return False

    monkeypatch.setattr(vehicles, '_recount_slot', TakenSlot())
    monkeypatch.setattr(vehicles, 'RECOUNT_WAIT_SECONDS', 0.1)
    agencies = ['krk:302', 'kml:1', 'krk:other']
    for agency_id in agencies:
        cache.set(
            f'planner:running:{graph.id}:{agency_id}:previous',
            {'agency': {'id': agency_id, 'name': 'x'}, 'trips': []},
        )

    assert len(get({'agencies': agencies}).json()['results']) == 3
    assert len(waits) == 3
    assert sum(waits) < 0.15  # one budget for the request, not 0.1 s per operator


def test_at_most_20_operators_per_request(otp):
    response = get({'agencies': [f'krk:{n}' for n in range(21)]})

    assert (response.status_code, response.json()['details']['fields']) == (
        400,
        {'agencies': ['max_length']},
    )
    assert otp.calls == []


def test_trips_otp_cannot_resolve_are_left_out(otp):
    # OTP fails on the start or end of a few trips (Warsaw on 2026-10-16) and answers
    # null for their pattern; the operator's other patterns still count.
    routes = [*MPK['agency']['routes'], {'patterns': [None]}]
    otp.running_error = otp_runtime.OtpQueryError(
        ['Exception while fetching data : null'], {'agency': {**MPK['agency'], 'routes': routes}}
    )

    [row] = get({'agencies': 'krk:302'}).json()['results']

    assert (row['trips_under_way'], row['trips_with_position']) == (2, 1)


def test_an_operator_otp_cannot_resolve_fails_the_request(otp):
    otp.running_error = otp_runtime.OtpQueryError(
        ['Exception while fetching data : null'], {'agency': None}
    )

    response = get({'agencies': 'krk:302'})

    assert (response.status_code, response.json()['code']) == (503, 'planner.unavailable')


def test_no_served_graph_no_coverage(otp, graph):
    GraphBuild.objects.update(is_active=False)

    assert get({'agencies': 'krk:302'}).json()['results'] == []
