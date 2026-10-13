"""Bike and scooter sharing (GBFS): rental sources in builds, the map and trip planning."""

import copy
import json
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from data_manager.models import RealtimeEndpointRT, RealtimeSubmission, RealtimeSubmissionHistory
from planner import otp_config, otp_runtime, pipeline, planning, services
from planner.models import GraphBuild, PlannerRentalSource, PlannerSettings

pytestmark = pytest.mark.django_db

GBFS_URL = 'https://gbfs.example.org/oswiecim/gbfs.json'


@pytest.fixture
def make_rental(make_submission):
    def make(network='dott', *, url=GBFS_URL, hide_original=False, auth_type='', published=True):
        org = make_submission(network).transport_organization
        rts = RealtimeSubmission.objects.create(
            transport_organization=org, protocol=RealtimeSubmission.PROTOCOL_GBFS, name=network
        )
        if url:
            RealtimeEndpointRT.objects.create(
                submission=rts,
                endpoint_type='gbfs',
                url=url,
                hide_original=hide_original or bool(auth_type),
                auth_type=auth_type,
                interval=60,
            )
        if published:
            RealtimeSubmissionHistory.objects.create(
                submission=rts,
                event_type=RealtimeSubmissionHistory.EVENT_COMPLETED,
                stage_before=3,
                stage_after=4,
            )
        return PlannerRentalSource.objects.create(submission=rts, network=network)

    return make


# ---------------------------------------------------------------------------
# Build configuration
# ---------------------------------------------------------------------------


def test_published_gbfs_becomes_a_vehicle_rental_updater(make_rental, fake_dns):
    source = make_rental('dott')

    updaters, results = otp_config.rental_updaters([source])

    assert updaters == [
        {
            'type': 'vehicle-rental',
            'sourceType': 'gbfs',
            'network': 'dott',
            'frequency': 'PT60S',
            'url': GBFS_URL,
        }
    ]
    assert results == [
        {
            'rental_source': source.id,
            'network': 'dott',
            'submission': source.submission_id,
            'outcome': 'included',
            'error_code': '',
        }
    ]


def test_proxied_gbfs_is_read_from_this_app(make_rental, settings):
    settings.PLANNER_ODT_INTERNAL_URL = 'http://odt-web:8000'
    source = make_rental('dott', hide_original=True)

    [updater], _ = otp_config.rental_updaters([source])

    assert updater['url'] == f'http://odt-web:8000/feed/rt/{source.submission_id}/gbfs/'
    assert updater['headers'] == {'X-ODT-Internal-Token': '${PLANNER_INTERNAL_TOKEN}'}


@pytest.mark.parametrize(
    ('kwargs', 'code'),
    [
        ({'published': False}, 'planner.rental_source_not_published'),
        ({'url': None}, 'planner.rental_source_without_gbfs'),
        # OTP follows the provider's links inside gbfs.json, which would need them too.
        ({'auth_type': 'api_key'}, 'planner.rental_source_requires_auth'),
        (
            {'url': 'https://user:pass@gbfs.example.org/gbfs.json'},
            'planner.rental_source_requires_auth',
        ),
        ({'url': 'https://intranet.example.org/gbfs.json'}, 'planner.rt_url_blocked'),
    ],
)
def test_unusable_sources_are_skipped_with_a_code(make_rental, fake_dns, kwargs, code):
    fake_dns['intranet.example.org'] = '10.0.0.5'
    source = make_rental('dott', **kwargs)

    updaters, [result] = otp_config.rental_updaters([source])

    assert updaters == []
    assert (result['outcome'], result['error_code']) == ('skipped', code)


def test_a_build_configures_enabled_rental_sources(make_feed, make_rental, fake_otp, fake_dns):
    make_feed('krk')
    source = make_rental('dott')
    PlannerRentalSource.objects.filter(pk=make_rental('off').pk).update(is_enabled=False)
    build = services.request_build(GraphBuild.TRIGGER_MANUAL)

    pipeline.run_build(build.id)

    build.refresh_from_db()
    assert build.status == GraphBuild.STATUS_SUCCEEDED
    assert [(r['network'], r['outcome']) for r in build.rental_results] == [('dott', 'included')]
    config = json.loads(Path(pipeline.graph_dir(build), 'router-config.json').read_text())
    assert [u['network'] for u in config['updaters'] if u['type'] == 'vehicle-rental'] == ['dott']
    assert source.id == build.rental_results[0]['rental_source']


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


def test_admin_adds_a_published_gbfs_feed_but_never_renames_its_network(
    make_rental, client_as, make_user
):
    source = make_rental('dott')
    other = source.submission
    source.delete()
    admin = client_as(make_user('Admin'))

    created = admin.post(
        '/api/planner/admin/rental-sources/',
        {'submission': other.id, 'network': 'dott'},
        format='json',
    )
    renamed = admin.patch(
        f"/api/planner/admin/rental-sources/{created.json()['id']}/",
        {'network': 'other'},
        format='json',
    )
    candidates = admin.get('/api/planner/admin/rental-candidates/').json()

    assert created.status_code == 201
    assert renamed.data['network'][0].code == 'planner.network_immutable'
    assert [(c['id'], c['rental_source']) for c in candidates['results']] == [
        (other.id, created.json()['id'])
    ]


def test_only_admins_manage_rental_sources(client_as, make_user):
    helper = client_as(make_user('Helper'))

    assert helper.get('/api/planner/admin/rental-sources/').status_code == 403


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

RENTALS = {
    'vehicleRentalsByBbox': [
        {
            '__typename': 'VehicleRentalStation',
            'stationId': 'dott:hub-20',
            'name': '20',
            'lat': 50.0385,
            'lon': 19.2392,
            'allowPickupNow': False,
            'allowDropoffNow': True,
            'realtime': True,
            'rentalNetwork': {'networkId': 'dott'},
            'availableVehicles': {
                'total': 0,
                'byType': [
                    {
                        'count': 0,
                        'vehicleType': {'formFactor': 'SCOOTER', 'propulsionType': 'ELECTRIC'},
                    }
                ],
            },
            'availableSpaces': {'total': 2147483647},
        },
        {
            '__typename': 'RentalVehicle',
            'vehicleId': 'dott:v1',
            'name': 'Default vehicle type',
            'lat': 50.0555,
            'lon': 19.2271,
            'allowPickupNow': True,
            'rentalNetwork': {'networkId': 'dott'},
            'vehicleType': {'formFactor': 'SCOOTER', 'propulsionType': 'ELECTRIC'},
            'fuel': {'percent': 0.52, 'range': 9240},
        },
        {
            '__typename': 'RentalVehicle',
            'vehicleId': 'veturilo:b7',
            'name': 'Rower',
            'lat': 50.05,
            'lon': 19.22,
            'allowPickupNow': True,
            'rentalNetwork': {'networkId': 'veturilo'},
            'vehicleType': {'formFactor': 'BICYCLE', 'propulsionType': 'HUMAN'},
            'fuel': None,
        },
    ]
}


@pytest.fixture
def otp(monkeypatch):
    PlannerSettings.objects.update_or_create(pk=1, defaults={'public_api_enabled': True})

    class Fake:
        answer = RENTALS
        calls = []

        def query(self, document, variables=None, timeout=None):
            self.calls.append(variables)
            return copy.deepcopy(self.answer)

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(otp_runtime, 'query', fake.query)
    return fake


def get(path, params):
    return APIClient().get(f'/api/planner/v1/{path}', params, REMOTE_ADDR='198.51.100.7')


def test_rental_map_shows_stations_and_free_vehicles(otp):
    body = get('rentals/', {'bbox': '19.18,50.0,19.3,50.07'}).json()

    assert otp.calls == [{'minLat': 50.0, 'minLon': 19.18, 'maxLat': 50.07, 'maxLon': 19.3}]
    station, scooter, bike = body['results']
    assert station == {
        'kind': 'station',
        'id': 'dott:hub-20',
        'network': 'dott',
        'name': '20',
        'lat': 50.0385,
        'lon': 19.2392,
        'vehicles_available': 0,
        'spaces_available': None,  # no dock limit
        'vehicle_types': [{'form_factor': 'scooter', 'propulsion': 'electric', 'count': 0}],
        'pickup_now': False,
        'dropoff_now': True,
        'battery_percent': None,
        'range_m': None,
    }
    assert (scooter['id'], scooter['battery_percent'], scooter['range_m']) == ('dott:v1', 52, 9240)
    assert bike['vehicle_types'] == [{'form_factor': 'bicycle', 'propulsion': 'human', 'count': 1}]


def test_rental_map_filters_by_system_and_vehicle_kind(otp):
    def ids(params):
        return [
            row['id']
            for row in get('rentals/', {'bbox': '19,50,19.5,50.4', **params}).json()['results']
        ]

    assert ids({'networks': 'veturilo'}) == ['veturilo:b7']
    assert ids({'form_factors': 'scooter'}) == ['dott:hub-20', 'dott:v1']


def test_plan_may_use_shared_scooters_and_bikes():
    variables = planning.plan_variables(
        {
            'from': '50.04,19.2',
            'to': '50.03,19.24',
            'wheelchair': False,
            'walk_speed': 'normal',
            'realtime': True,
            'min_transfer_minutes': 2,
            'num_itineraries': 3,
            'stops': False,
            'geometry': False,
            'arrive_by': False,
            'modes': ['bus'],
            'rental': ['scooter', 'bicycle'],
        }
    )

    street = ['WALK', 'SCOOTER_RENTAL', 'BICYCLE_RENTAL']
    assert variables['modes'] == {
        'direct': street,
        'transit': {'transit': [{'mode': 'BUS'}], 'access': street, 'egress': street},
    }


def test_admin_rejects_a_source_otp_could_not_read(make_rental, client_as, make_user):
    locked = make_rental('locked', auth_type='api_key')
    bare = make_rental('bare', url=None)
    submissions = [locked.submission_id, bare.submission_id]
    PlannerRentalSource.objects.all().delete()
    admin = client_as(make_user('Admin'))

    codes = [
        admin.post(
            '/api/planner/admin/rental-sources/',
            {'submission': sid, 'network': 'n1'},
            format='json',
        )
        .data['submission'][0]
        .code
        for sid in submissions
    ]

    assert codes == ['planner.rental_source_requires_auth', 'planner.rental_source_without_gbfs']


def test_rollback_keeps_the_served_graphs_rental_results(make_rental, settings):
    from django.utils import timezone

    source = make_rental('dott')
    target = GraphBuild.objects.create(
        trigger='manual',
        otp_image=settings.PLANNER_OTP_IMAGE,
        status=GraphBuild.STATUS_SUCCEEDED,
        finished_at=timezone.now(),
        rental_results=[{'rental_source': source.id, 'network': 'dott', 'outcome': 'included'}],
    )
    graph = Path(pipeline.graph_dir(target), 'graph.obj')
    graph.parent.mkdir(parents=True)
    graph.write_text('x')

    rollback = services.request_rollback(target)

    assert rollback.rental_results == target.rental_results


def test_status_names_the_sharing_systems_and_their_licences(make_rental):
    source = make_rental('dott')
    source.submission.license = 'CC0-1.0'
    source.submission.save(update_fields=['license'])
    PlannerSettings.objects.update_or_create(pk=1, defaults={'public_api_enabled': True})
    GraphBuild.objects.create(
        trigger='manual',
        otp_image='x:2.10.0',
        status=GraphBuild.STATUS_SUCCEEDED,
        is_active=True,
        rental_results=[
            {'rental_source': source.id, 'network': 'dott', 'outcome': 'included'},
            {'rental_source': 999, 'network': 'gone', 'outcome': 'skipped'},
        ],
    )

    networks = get('status/', {}).json()['rental_networks']

    assert networks == [
        {
            'network': 'dott',
            'organization': {
                'id': source.submission.transport_organization_id,
                'name': 'Org dott',
                'region': 'Region',
            },
            'license': 'CC0-1.0',
        }
    ]


def test_scooters_include_standing_and_seated_ones(otp):
    otp.answer = copy.deepcopy(RENTALS)
    otp.answer['vehicleRentalsByBbox'][1]['vehicleType']['formFactor'] = 'SCOOTER_STANDING'

    body = get('rentals/', {'bbox': '19,50,19.5,50.4', 'form_factors': 'scooter'}).json()

    assert [row['id'] for row in body['results']] == ['dott:hub-20', 'dott:v1']
