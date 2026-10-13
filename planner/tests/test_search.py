"""The search index (copied from OTP) and the stop, line and operator lookups."""

import copy

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from planner import otp_runtime, pipeline, search_index, tasks
from planner.models import (
    GraphBuild,
    IndexedAgency,
    IndexedRoute,
    IndexedStop,
    IndexedStopGroup,
    PlannerSettings,
)

pytestmark = pytest.mark.django_db

V1 = '/api/planner/v1/'


def stop(stop_id, name, lat, lon, routes, mode='BUS', **extra):
    return {
        'gtfsId': stop_id,
        'name': name,
        'code': None,
        'platformCode': None,
        'lat': lat,
        'lon': lon,
        'vehicleMode': mode,
        'parentStation': None,
        'routes': [{'gtfsId': route} for route in routes],
        **extra,
    }


def route(route_id, short, mode, agency, long_name=None):
    return {
        'gtfsId': route_id,
        'shortName': short,
        'longName': long_name,
        'mode': mode,
        'color': None,
        'textColor': None,
        'agency': {'gtfsId': agency},
    }


# Shaped like OTP 2.10's answer to search_index.INDEX_DOCUMENT.
OTP_INDEX = {
    'feeds': [
        {'feedId': 'krk', 'agencies': [{'gtfsId': 'krk:302', 'name': 'MPK Kraków', 'url': None}]},
        {'feedId': 'kml', 'agencies': [{'gtfsId': 'kml:1', 'name': 'Koleje Małopolskie'}]},
    ],
    'routes': [
        route('krk:70', '70', 'TRAM', 'krk:302'),
        route('krk:152', '152', 'BUS', 'krk:302'),
        route('krk:704', '704', 'BUS', 'krk:302'),
        route('kml:a1', 'A1', 'BUS', 'kml:1', 'Kraków - Wieliczka'),
        {**route('krk:99', '99', 'BUS', 'krk:302'), 'agency': None},  # skipped
    ],
    'stops': [
        # Two poles 70 m apart: one place.
        stop('krk:1', 'Rondo Mogilskie', 50.0650, 19.9580, ['krk:70'], 'TRAM'),
        stop('krk:2', 'Rondo Mogilskie', 50.0652, 19.9590, ['krk:152']),
        # Same name 5 km away: another place.
        stop('krk:3', 'Rondo Mogilskie', 50.1100, 19.9000, ['krk:704']),
        stop('krk:4', 'Rondo Grunwaldzkie', 50.0480, 19.9350, ['krk:70'], 'TRAM'),
        stop('krk:5', 'Teatr Bagatela', 50.0631, 19.9326, ['krk:70', 'krk:152']),
        stop(
            'kml:10',
            'Wieliczka Rynek',
            49.9870,
            20.0650,
            ['kml:a1'],
            parentStation={'gtfsId': 'kml:st1'},
        ),
        stop('kml:11', 'Łódź Kaliska', 51.7570, 19.4300, ['kml:a1']),
        # A place served by two operators.
        stop('krk:6', 'Nowy Kleparz', 50.0900, 19.9200, ['krk:70'], 'TRAM'),
        stop('kml:12', 'Nowy Kleparz', 50.0901, 19.9201, ['kml:a1']),
        # No line calls here: not indexed.
        stop('krk:7', 'Zajezdnia', 50.0300, 19.9000, []),
    ],
}


@pytest.fixture
def otp_index(monkeypatch):
    """OTP answering the index query with ``otp_index.answer`` (or raising ``otp_index.error``)."""

    class Fake:
        answer = copy.deepcopy(OTP_INDEX)
        error = None

        def query(self, document, variables=None, timeout=None):
            if self.error:
                raise self.error
            return copy.deepcopy(self.answer)

    fake = Fake()
    monkeypatch.setattr(otp_runtime, 'query', fake.query)
    return fake


@pytest.fixture
def active_build():
    return GraphBuild.objects.create(
        trigger='manual',
        otp_image='opentripplanner/opentripplanner:2.10.0',
        status=GraphBuild.STATUS_SUCCEEDED,
        is_active=True,
        finished_at=timezone.now(),
    )


@pytest.fixture
def indexed(otp_index, active_build, make_feed):
    """The index built from OTP_INDEX, with feed krk published by an ODT organization."""
    feed = make_feed('krk')
    PlannerSettings.objects.update_or_create(pk=1, defaults={'public_api_enabled': True})
    assert search_index.refresh_active()
    return feed.submission.transport_organization


def get(path, params=None):
    return APIClient().get(f'{V1}{path}', params or {}, REMOTE_ADDR='198.51.100.7')


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('name', 'text'),
    [
        ('Kraków Główny', 'krakow glowny'),
        ('ŁÓDŹ Fabryczna', 'lodz fabryczna'),
        ('Os. Kolorowe (n/ż)', 'os kolorowe n z'),
        ('  Rondo   Mogilskie ', 'rondo mogilskie'),
    ],
)
def test_names_are_compared_without_case_diacritics_or_punctuation(name, text):
    assert search_index.search_text(name) == text


def test_index_copies_the_active_graph_once(indexed, active_build):
    active_build.refresh_from_db()

    assert active_build.indexed_at is not None
    assert search_index.refresh_active() is False
    assert IndexedAgency.objects.get(agency_id='krk:302').organization == indexed
    assert IndexedAgency.objects.get(agency_id='kml:1').organization is None
    tram = IndexedRoute.objects.get(route_id='krk:70')
    assert (tram.mode, tram.agency.agency_id, tram.color) == ('tram', 'krk:302', '')
    # The line's position is the centre of its stops.
    assert tram.lat == pytest.approx((50.0650 + 50.0480 + 50.0631 + 50.0900) / 4)
    assert not IndexedRoute.objects.filter(route_id='krk:99').exists()
    assert not IndexedStop.objects.filter(stop_id='krk:7').exists()
    mogilskie = IndexedStopGroup.objects.filter(name='Rondo Mogilskie').order_by('lat')
    assert [sorted(g.stops.values_list('stop_id', flat=True)) for g in mogilskie] == [
        ['krk:1', 'krk:2'],
        ['krk:3'],
    ]
    assert (mogilskie[0].modes, mogilskie[0].agency_ids, mogilskie[0].route_count) == (
        ['bus', 'tram'],
        ['krk:302'],
        2,
    )
    assert IndexedStop.objects.get(stop_id='kml:10').station_id == 'kml:st1'


def test_lines_sort_by_number_then_name(indexed):
    assert list(IndexedRoute.objects.values_list('short_name', flat=True)) == [
        '70',
        '152',
        '704',
        'A1',
    ]


def test_a_new_graph_replaces_the_whole_index(indexed, otp_index):
    otp_index.answer['stops'] = otp_index.answer['stops'][:1]
    otp_index.answer['routes'] = otp_index.answer['routes'][:1]
    GraphBuild.objects.update(is_active=False)
    GraphBuild.objects.create(
        trigger='rollback', otp_image='x', status=GraphBuild.STATUS_SUCCEEDED, is_active=True
    )

    assert search_index.refresh_active()

    assert list(IndexedStop.objects.values_list('stop_id', flat=True)) == ['krk:1']
    assert IndexedStopGroup.objects.count() == 1
    assert list(IndexedRoute.objects.values_list('route_id', flat=True)) == ['krk:70']


def test_otp_down_keeps_the_old_index_and_retries_later(indexed, otp_index):
    GraphBuild.objects.update(indexed_at=None)
    otp_index.error = otp_runtime.OtpUnavailable('refused')

    pipeline.refresh_index()  # logs, never raises

    assert IndexedStop.objects.count() == len(OTP_INDEX['stops']) - 1  # all served stops
    assert GraphBuild.objects.get(is_active=True).indexed_at is None


def test_watchdog_queues_indexing_of_an_unindexed_active_graph(active_build, monkeypatch):
    queued = []
    monkeypatch.setattr(tasks, 'enqueue', lambda task, *args: queued.append(task.name))
    monkeypatch.setattr(pipeline, 'fail_stale_builds', lambda: 0)

    tasks.watchdog_task()
    GraphBuild.objects.update(indexed_at=timezone.now())
    tasks.watchdog_task()

    assert queued == ['planner.ensure_serving', 'planner.refresh_index', 'planner.ensure_serving']


# ---------------------------------------------------------------------------
# Stops
# ---------------------------------------------------------------------------


def names(response):
    return [row['name'] for row in response.json()['results']]


def test_stop_search_ignores_polish_letters(indexed):
    assert names(get('stops/search/', {'q': 'lodz'})) == ['Łódź Kaliska']


def test_stop_search_returns_places_with_their_poles_and_lines(indexed):
    body = get('stops/search/', {'q': 'rondo mog'}).json()

    # Fuzzy matching also finds other roundabouts, after the close matches.
    assert names(get('stops/search/', {'q': 'rondo mog'})) == [
        'Rondo Mogilskie',
        'Rondo Mogilskie',
        'Rondo Grunwaldzkie',
    ]
    first = body['results'][0]
    assert first['name'] == 'Rondo Mogilskie'
    assert [s['id'] for s in first['stops']] == ['krk:1', 'krk:2']
    assert [r['short_name'] for r in first['routes']] == ['152', '70']  # bus before tram
    assert first['routes'][1] == {
        'id': 'krk:70',
        'short_name': '70',
        'long_name': None,
        'mode': 'tram',
        'color': None,
        'text_color': None,
        'agency': {'id': 'krk:302', 'name': 'MPK Kraków'},
    }
    assert first['distance_m'] is None


def test_stop_search_ranks_the_place_near_the_user_first(indexed):
    body = get('stops/search/', {'q': 'rondo mogilskie', 'near': '50.11,19.90'}).json()

    assert [row['stops'][0]['id'] for row in body['results']] == ['krk:3', 'krk:1']
    assert body['results'][0]['distance_m'] < 100


def test_stop_search_filters_by_mode_operator_and_organization(indexed):
    assert names(get('stops/search/', {'q': 'rondo', 'modes': 'tram'})) == [
        'Rondo Mogilskie',
        'Rondo Grunwaldzkie',
    ]
    assert names(get('stops/search/', {'q': 'wieliczka', 'agencies': 'kml:1'})) == [
        'Wieliczka Rynek'
    ]
    assert names(get('stops/search/', {'q': 'wieliczka', 'organizations': indexed.id})) == []


@pytest.mark.parametrize(
    ('params', 'fields'),
    [
        ({'q': 'ro'}, {'q': ['min_length']}),
        ({'q': '!!!'}, {'q': ['min_length']}),
        (
            {'q': 'rondo', 'organizations': '999'},
            {'organizations': ['planner.organization_not_found']},
        ),
        ({'q': 'rondo', 'near': '50.1'}, {'near': ['planner.invalid_location']}),
        ({'q': 'rondo', 'agencies': '302'}, {'agencies': {'0': ['planner.invalid_id']}}),
    ],
)
def test_stop_search_parameter_errors(indexed, params, fields):
    response = get('stops/search/', params)

    assert response.status_code == 400
    assert response.json()['details']['fields'] == fields


def test_stops_nearby_are_ordered_by_distance(indexed):
    def nearby(radius):
        body = get('stops/nearby/', {'near': '50.0640,19.9500', 'radius': radius}).json()
        return [(row['name'], row['distance_m']) for row in body['results']]

    assert nearby(1300) == [('Rondo Mogilskie', 619), ('Teatr Bagatela', 1246)]
    assert nearby(1000) == [('Rondo Mogilskie', 619)]


def test_stops_in_a_map_area_are_single_poles(indexed):
    body = get('stops/', {'bbox': '19.95,50.06,19.96,50.07'}).json()

    assert [row['id'] for row in body['results']] == ['krk:1', 'krk:2']
    assert body['results'][0] == {
        'id': 'krk:1',
        'name': 'Rondo Mogilskie',
        'code': None,
        'platform': None,
        'lat': 50.065,
        'lon': 19.958,
        'mode': 'tram',
        'station_id': None,
    }


@pytest.mark.parametrize(
    ('bbox', 'code'),
    [
        ('19,50,20,51', 'planner.bbox_too_large'),
        ('19.96,50.06,19.95,50.07', 'planner.invalid_bbox'),
        ('19.95,50.06,19.96', 'planner.invalid_bbox'),
    ],
)
def test_map_area_must_be_small_and_well_formed(indexed, bbox, code):
    response = get('stops/', {'bbox': bbox})

    assert response.json()['details']['fields'] == {'bbox': [code]}


def test_map_area_filters_operators_per_pole(indexed):
    bbox = '19.91,50.08,19.93,50.10'

    assert [row['id'] for row in get('stops/', {'bbox': bbox}).json()['results']] == [
        'kml:12',
        'krk:6',
    ]
    assert [
        row['id'] for row in get('stops/', {'bbox': bbox, 'agencies': 'kml:1'}).json()['results']
    ] == ['kml:12']


def test_one_stop_with_its_lines_and_the_other_poles(indexed):
    body = get('stop/', {'id': 'krk:1'}).json()

    assert (body['id'], body['name'], body['mode']) == ('krk:1', 'Rondo Mogilskie', 'tram')
    assert [r['id'] for r in body['routes']] == ['krk:70']
    assert [s['id'] for s in body['group']['stops']] == ['krk:1', 'krk:2']


def test_unknown_stop_is_404(indexed):
    response = get('stop/', {'id': 'krk:nope'})

    assert (response.status_code, response.json()) == (
        404,
        {'code': 'planner.stop_not_found', 'details': {}},
    )


def test_stop_search_uses_a_fixed_number_of_queries(indexed, django_assert_max_num_queries):
    # Settings and bans are cached after the first request.
    get('stops/search/', {'q': 'rondo'})
    with django_assert_max_num_queries(6):
        assert get('stops/search/', {'q': 'rondo'}).json()['count'] == 3


# ---------------------------------------------------------------------------
# Lines and operators
# ---------------------------------------------------------------------------


def line_names(response):
    return [row['short_name'] for row in response.json()['results']]


def test_line_search_puts_the_exact_number_first(indexed):
    assert line_names(get('routes/', {'q': '70'})) == ['70', '704']


def test_line_search_by_name_and_filters(indexed):
    assert line_names(get('routes/', {'q': 'wieliczka'})) == ['A1']
    assert line_names(get('routes/', {'modes': 'tram'})) == ['70']
    assert line_names(get('routes/', {'organizations': indexed.id})) == ['70', '152', '704']


def test_line_search_needs_a_letter_or_digit(indexed):
    assert get('routes/', {'q': '--'}).json()['details']['fields'] == {'q': ['min_length']}


def test_lines_near_a_point_come_first(indexed):
    assert line_names(get('routes/', {'near': '50.11,19.90'}))[0] == '704'


def test_operators_name_their_odt_organization(indexed):
    body = get('agencies/').json()

    assert body['results'] == [
        {
            'id': 'kml:1',
            'name': 'Koleje Małopolskie',
            'url': None,
            'feed_id': 'kml',
            'organization': None,
            'route_count': 1,
        },
        {
            'id': 'krk:302',
            'name': 'MPK Kraków',
            'url': None,
            'feed_id': 'krk',
            'organization': {'id': indexed.id, 'name': 'Org krk', 'region': 'Region'},
            'route_count': 3,
        },
    ]
    filtered = get('agencies/', {'organizations': indexed.id}).json()['results']
    assert [row['id'] for row in filtered] == ['krk:302']
    unknown = get('agencies/', {'organizations': 999}).json()
    assert unknown['details']['fields'] == {'organizations': ['planner.organization_not_found']}


def test_lookups_answer_while_otp_is_stopped_for_a_build(indexed):
    GraphBuild.objects.create(
        trigger='manual',
        otp_image='x',
        status=GraphBuild.STATUS_RUNNING,
        otp_stopped_at=timezone.now(),
    )

    assert get('stops/search/', {'q': 'rondo'}).status_code == 200
    assert get('plan/', {'from': 'krk:1', 'to': 'krk:4'}).status_code == 503
