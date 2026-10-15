"""The search index (copied from OTP) and the stop, line and operator lookups."""

import copy

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from planner import otp_runtime, pipeline, search_index, tasks
from planner.models import (
    GraphBuild,
    IndexedAgency,
    IndexedBlockTrip,
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
        trips = {
            'krk': [
                {'gtfsId': 'krk:t1', 'blockId': 'T/70-08'},
                {'gtfsId': 'krk:t2', 'blockId': 'T/70-08'},
                {'gtfsId': 'krk:t3', 'blockId': None},
            ],
        }
        # Tram 70 leaves Rondo Mogilskie (krk:1) eastwards along its path.
        patterns = {
            'krk': [
                {
                    'stops': [
                        {'gtfsId': 'krk:1', 'lat': 50.0650, 'lon': 19.9580},
                        {'gtfsId': 'krk:4', 'lat': 50.0480, 'lon': 19.9350},
                    ],
                    'patternGeometry': {
                        'points': search_index_encode(
                            [(50.0650, 19.9580), (50.0650, 19.9600), (50.0480, 19.9350)]
                        )
                    },
                }
            ]
        }
        error = None

        def query(self, document, variables=None, timeout=None):
            if self.error:
                raise self.error
            if document == search_index.BLOCK_TRIPS_DOCUMENT:
                return {'trips': self.trips.get(variables['feeds'][0], [])}
            if document == search_index.PATTERNS_DOCUMENT:
                return {'routes': [{'patterns': self.patterns.get(variables['feeds'][0], [])}]}
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


def search_index_encode(points):
    """Google's polyline encoding, for paths in the fake OTP answers."""
    out, last = [], (0, 0)
    for lat, lon in points:
        for value, previous in zip((round(lat * 1e5), round(lon * 1e5)), last, strict=True):
            delta = value - previous
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                out.append(chr((0x20 | (delta & 0x1F)) + 63))
                delta >>= 5
            out.append(chr(delta + 63))
        last = (round(lat * 1e5), round(lon * 1e5))
    return ''.join(out)


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


def test_index_keeps_which_trips_share_a_block(indexed):
    assert sorted(IndexedBlockTrip.objects.values_list('feed_id', 'block_id', 'trip_id')) == [
        ('krk', 'T/70-08', 'krk:t1'),
        ('krk', 'T/70-08', 'krk:t2'),
    ]


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


def test_stop_search_returns_places_with_their_poles_and_no_lines(indexed):
    body = get('stops/search/', {'q': 'rondo mog'}).json()

    # Fuzzy matching also finds other roundabouts, after the close matches.
    assert names(get('stops/search/', {'q': 'rondo mog'})) == [
        'Rondo Mogilskie',
        'Rondo Mogilskie',
        'Rondo Grunwaldzkie',
    ]
    first = body['results'][0]
    assert list(first) == ['name', 'lat', 'lon', 'modes', 'distance_m', 'stops']
    assert (first['name'], first['modes'], first['distance_m']) == (
        'Rondo Mogilskie',
        ['bus', 'tram'],
        None,
    )
    assert [s['id'] for s in first['stops']] == ['krk:1', 'krk:2']


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


# Nowy Kleparz has a tram pole of MPK (krk:6) and a bus pole of KML (kml:12).
@pytest.mark.parametrize(
    'place', [('stops/search/', {'q': 'nowy kleparz'}), ('stops/nearby/', {'near': '50.09,19.92'})]
)
@pytest.mark.parametrize(
    ('filters', 'places'),
    [
        ({}, [(['kml:12', 'krk:6'], ['bus', 'tram'])]),
        ({'agencies': 'kml:1'}, [(['kml:12'], ['bus'])]),
        ({'agencies': 'krk:302'}, [(['krk:6'], ['tram'])]),
        ({'modes': 'tram'}, [(['krk:6'], ['tram'])]),
        # Each filter matches a pole, but no pole matches both: no place.
        ({'modes': 'tram', 'agencies': 'kml:1'}, []),
    ],
)
def test_place_filters_keep_only_the_poles_that_match(indexed, place, filters, places):
    path, params = place

    results = get(path, {**params, **filters}).json()['results']

    assert [([s['id'] for s in row['stops']], row['modes']) for row in results] == places


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
        'heading': 90,
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


def test_lines_of_one_stop_come_by_mode_then_number(indexed):
    body = get('stop/', {'id': 'krk:5'}).json()

    assert [r['short_name'] for r in body['routes']] == ['152', '70']  # bus before tram


def test_unknown_stop_is_404(indexed):
    response = get('stop/', {'id': 'krk:nope'})

    assert (response.status_code, response.json()) == (
        404,
        {'code': 'planner.stop_not_found', 'details': {}},
    )


@pytest.mark.parametrize(
    ('filters', 'count'), [({}, 3), ({'agencies': 'krk:302', 'modes': 'tram'}, 2)]
)
def test_stop_search_uses_a_fixed_number_of_queries(
    indexed, django_assert_max_num_queries, filters, count
):
    # Settings and bans are cached after the first request.
    get('stops/search/', {'q': 'rondo', **filters})
    with django_assert_max_num_queries(6):
        assert get('stops/search/', {'q': 'rondo', **filters}).json()['count'] == count


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


def test_lines_of_a_place_are_those_calling_at_its_poles(indexed):
    rondo = {'stops': ['krk:1', 'krk:2']}
    kleparz = {'stops': ['krk:6', 'kml:12']}

    assert line_names(get('routes/', rondo)) == ['70', '152']
    assert get('routes/', rondo).json()['results'][0] == {
        'id': 'krk:70',
        'short_name': '70',
        'long_name': None,
        'mode': 'tram',
        'color': None,
        'text_color': None,
        'agency': {'id': 'krk:302', 'name': 'MPK Kraków'},
    }
    assert line_names(get('routes/', kleparz)) == ['70', 'A1']
    assert line_names(get('routes/', {**kleparz, 'agencies': 'kml:1'})) == ['A1']
    assert line_names(get('routes/', {**kleparz, 'q': '70'})) == ['70']
    assert line_names(get('routes/', {'stops': 'krk:nope'})) == []


@pytest.mark.parametrize(
    ('stops', 'fields'),
    [
        (['nope'], {'stops': {'0': ['planner.invalid_id']}}),
        ([f'krk:{n}' for n in range(51)], {'stops': ['max_length']}),
    ],
)
def test_lines_of_stops_parameter_errors(indexed, stops, fields):
    response = get('routes/', {'stops': stops})

    assert response.status_code == 400
    assert response.json()['details']['fields'] == fields


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


# ---------------------------------------------------------------------------
# Stop headings (direction arrows)
# ---------------------------------------------------------------------------


def test_polylines_decode_as_otp_writes_them():
    # Google's reference example.
    assert search_index.decode_polyline('_p~iF~ps|U_ulLnnqC_mqNvxq`@') == [
        (38.5, -120.2),
        (40.7, -120.95),
        (43.252, -126.453),
    ]


def pole(stop_id, lon):
    return {'gtfsId': stop_id, 'lat': 50.0, 'lon': lon}


def headings_for(monkeypatch, *patterns):
    monkeypatch.setattr(
        otp_runtime,
        'query',
        lambda document, variables=None, timeout=None: {'routes': [{'patterns': list(patterns)}]},
    )
    return search_index.stop_headings(['f'])


def test_a_pole_points_where_its_lines_leave_and_a_terminus_where_they_arrive(monkeypatch):
    eastbound = {'stops': [pole('f:a', 19.000), pole('f:b', 19.001), pole('f:end', 19.002)]}

    headings = headings_for(monkeypatch, eastbound)

    assert headings == {'f:a': 90, 'f:b': 90, 'f:end': 90}


def test_the_path_beats_a_straight_line_to_the_next_stop(monkeypatch):
    # Leaves north along the street, then turns east to the next stop.
    pattern = {
        'stops': [pole('f:a', 19.000), {'gtfsId': 'f:b', 'lat': 50.0005, 'lon': 19.002}],
        'patternGeometry': {
            'points': search_index_encode([(50.0, 19.0), (50.0005, 19.0), (50.0005, 19.002)])
        },
    }

    assert headings_for(monkeypatch, pattern)['f:a'] == 0


def test_a_pole_served_both_ways_gets_no_heading(monkeypatch):
    eastbound = {'stops': [pole('f:mid', 19.001), pole('f:east', 19.002)]}
    westbound = {'stops': [pole('f:mid', 19.001), pole('f:west', 19.000)]}

    headings = headings_for(monkeypatch, eastbound, westbound)

    assert 'f:mid' not in headings
    assert (headings['f:east'], headings['f:west']) == (90, 270)


def test_a_pole_just_before_a_turn_points_along_its_own_street(monkeypatch):
    # East along the street past the pole, then south at the corner 7 m further.
    pattern = {
        'stops': [
            {'gtfsId': 'f:a', 'lat': 50.0, 'lon': 19.0002},
            {'gtfsId': 'f:b', 'lat': 49.999, 'lon': 19.0003},
        ],
        'patternGeometry': {
            'points': search_index_encode([(50.0, 19.0), (50.0, 19.0003), (49.999, 19.0003)])
        },
    }

    assert headings_for(monkeypatch, pattern)['f:a'] == 90
