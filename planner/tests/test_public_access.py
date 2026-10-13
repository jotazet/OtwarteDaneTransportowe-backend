"""Public planner API access: kill switch, limits, keys, bans, maintenance, logs, CORS."""

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from planner import access
from planner.models import ApiKey, GraphBuild, IpBan, PlannerRequestLog, PlannerSettings

pytestmark = pytest.mark.django_db

LOOKUP = '/api/planner/v1/test-lookup/'
OTP = '/api/planner/v1/test-otp/'
STATUS = '/api/planner/v1/status/'


@pytest.fixture(autouse=True)
def probe_urls(settings):
    settings.ROOT_URLCONF = 'planner.tests.urls'


@pytest.fixture
def enabled():
    PlannerSettings.objects.update_or_create(pk=1, defaults={'public_api_enabled': True})
    return PlannerSettings.load()


def get(path, ip='198.51.100.7', **headers):
    return APIClient().get(path, REMOTE_ADDR=ip, headers=headers)


def make_key(raw='odt_secret', **fields):
    return ApiKey.objects.create(
        name='App', prefix=raw[:12], key_hash=access.hash_key(raw), **fields
    )


def test_api_is_off_until_switched_on_but_status_answers():
    assert get(LOOKUP).json() == {'code': 'planner.disabled', 'details': {}}
    assert get(LOOKUP).status_code == 503
    status = get(STATUS).json()
    assert (status['available'], status['reason']) == (False, 'disabled')


def test_status_lists_served_feeds_with_licences_and_osm_attribution(enabled, make_feed):
    feed = make_feed('krk')
    feed.submission.static_entry.license = 'CC-BY-4.0'
    feed.submission.static_entry.save(update_fields=['license'])
    GraphBuild.objects.create(
        trigger='manual',
        otp_image='opentripplanner/opentripplanner:2.10.0',
        status='succeeded',
        is_active=True,
        finished_at=timezone.now(),
        feed_results=[
            {'planner_feed': feed.id, 'feed_id': 'krk', 'outcome': 'stale', 'version_at': None},
        ],
    )

    status = get(STATUS).json()

    assert (status['available'], status['reason']) == (True, None)
    assert status['graph']['otp_version'] == '2.10.0'
    assert status['feeds'][0] | {'organization': None} == {
        'feed_id': 'krk',
        'organization': None,
        'license': 'CC-BY-4.0',
        'outcome': 'stale',
        'version_at': None,
    }
    assert status['attribution'][0]['license'] == 'ODbL-1.0'


def test_anonymous_clients_are_limited_per_ip_with_retry_after(enabled):
    PlannerSettings.objects.filter(pk=1).update(anonymous_lookup_per_minute=2)
    access.forget_cached_access()

    codes = [get(LOOKUP).status_code for _ in range(3)]
    limited = get(LOOKUP)
    other_ip = get(LOOKUP, ip='198.51.100.8')

    assert codes == [200, 200, 429]
    assert limited.json() == {'code': 'planner.rate_limited', 'details': {'scope': 'client'}}
    assert 0 < int(limited['Retry-After']) <= 60
    assert other_ip.status_code == 200


def test_ipv6_clients_share_a_limit_per_64(enabled):
    PlannerSettings.objects.filter(pk=1).update(anonymous_lookup_per_minute=1)
    access.forget_cached_access()

    first = get(LOOKUP, ip='2001:db8:1:2::1')
    same_64 = get(LOOKUP, ip='2001:db8:1:2::ffff')
    other_64 = get(LOOKUP, ip='2001:db8:1:3::1')

    assert (first.status_code, same_64.status_code, other_64.status_code) == (200, 429, 200)


def test_global_limit_covers_all_clients(enabled):
    PlannerSettings.objects.filter(pk=1).update(global_lookup_per_minute=2)
    access.forget_cached_access()

    responses = [get(LOOKUP, ip=f'198.51.100.{n}') for n in range(3)]

    assert responses[2].json()['details'] == {'scope': 'global'}


def test_api_key_raises_the_limit_and_can_override_it(enabled):
    PlannerSettings.objects.filter(pk=1).update(anonymous_lookup_per_minute=1)
    access.forget_cached_access()
    make_key('odt_one')
    make_key('odt_two', lookup_per_minute=1)

    keyed = [get(LOOKUP, **{'X-API-Key': 'odt_one'}).status_code for _ in range(3)]
    overridden = [get(LOOKUP, **{'X-API-Key': 'odt_two'}).status_code for _ in range(2)]

    assert keyed == [200, 200, 200]
    assert overridden == [200, 429]


@pytest.mark.parametrize('active', [True, False])
def test_unknown_or_revoked_key_is_refused(enabled, active):
    make_key('odt_one', is_active=active)

    response = get(LOOKUP, **{'X-API-Key': 'odt_one' if not active else 'odt_wrong'})

    assert response.status_code == 401
    assert response.json()['code'] == 'planner.invalid_api_key'


def test_used_key_records_last_use(enabled):
    key = make_key('odt_one')

    get(LOOKUP, **{'X-API-Key': 'odt_one'})

    key.refresh_from_db()
    assert key.last_used_at is not None


def test_banned_network_is_refused_and_expired_ban_ignored(enabled):
    IpBan.objects.create(network='203.0.113.0/24')
    IpBan.objects.create(network='192.0.2.1/32', expires_at=timezone.now() - timedelta(minutes=1))

    banned = get(LOOKUP, ip='203.0.113.50')
    expired = get(LOOKUP, ip='192.0.2.1')

    assert banned.status_code == 403
    assert banned.json()['code'] == 'planner.client_banned'
    assert expired.status_code == 200


def test_spoofed_forwarded_for_does_not_escape_a_ban(enabled, settings):
    IpBan.objects.create(network='203.0.113.50/32')

    response = APIClient().get(LOOKUP, REMOTE_ADDR='203.0.113.50', HTTP_X_FORWARDED_FOR='1.2.3.4')

    assert response.status_code == 403


def test_build_window_stops_only_endpoints_that_need_otp(enabled):
    GraphBuild.objects.create(
        trigger='schedule', otp_image='x', status='running', otp_stopped_at=timezone.now()
    )

    otp = get(OTP)

    assert otp.status_code == 503
    assert otp.json() == {'code': 'planner.unavailable', 'details': {'reason': 'maintenance'}}
    assert int(otp['Retry-After']) > 0
    assert get(LOOKUP).status_code == 200


def test_invalid_parameters_come_back_as_field_codes(enabled):
    response = get('/api/planner/v1/test-validate/?n=0')

    assert response.status_code == 400
    assert response.json() == {
        'code': 'planner.invalid_request',
        'details': {'fields': {'n': ['min_value']}},
    }


def test_other_drf_errors_use_the_code_shape(enabled):
    not_allowed = APIClient().post(LOOKUP, REMOTE_ADDR='198.51.100.7')

    assert not_allowed.status_code == 405
    assert not_allowed.json() == {'code': 'planner.method_not_allowed', 'details': {}}


def test_request_log_keeps_no_query_string_but_ip_and_key(enabled):
    make_key('odt_one')

    get(f'{LOOKUP}?from=50.06,19.93&to=50.07,20.03', **{'User-Agent': 'probe/1.0'})
    get(LOOKUP, **{'X-API-Key': 'odt_one'})

    anonymous, keyed = PlannerRequestLog.objects.order_by('id')
    assert (anonymous.endpoint, anonymous.status_code, anonymous.client_ip) == (
        'test-lookup',
        200,
        '198.51.100.7',
    )
    assert anonymous.user_agent == 'probe/1.0'
    assert '50.06' not in str(PlannerRequestLog.objects.values())
    assert keyed.api_key.name == 'App'
    assert keyed.client_ip == '198.51.100.7'


def test_rejections_are_logged_once_a_minute_per_client(enabled):
    PlannerSettings.objects.filter(pk=1).update(anonymous_lookup_per_minute=1)
    access.forget_cached_access()

    for _ in range(5):
        get(LOOKUP)

    assert PlannerRequestLog.objects.filter(status_code=429).count() == 1


def test_a_failing_log_write_does_not_break_the_response(enabled, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError('database gone')

    monkeypatch.setattr(PlannerRequestLog.objects, 'create', broken)

    assert get(LOOKUP).status_code == 200


def test_cors_allows_any_origin_only_for_the_public_planner_api(enabled):
    planner = APIClient().get(STATUS, HTTP_ORIGIN='https://gmina.example.pl')
    cases = APIClient().get('/api/cases/data-providers/', HTTP_ORIGIN='https://gmina.example.pl')
    preflight = APIClient().options(
        STATUS,
        HTTP_ORIGIN='https://gmina.example.pl',
        HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS='x-api-key',
    )

    assert planner['Access-Control-Allow-Origin'] == 'https://gmina.example.pl'
    assert 'Retry-After' in planner['Access-Control-Expose-Headers']
    assert 'Access-Control-Allow-Credentials' not in planner
    assert 'Access-Control-Allow-Origin' not in cases
    assert 'x-api-key' in preflight['Access-Control-Allow-Headers']


def test_public_schema_is_open_and_lists_only_planner_routes():
    schema = APIClient().get('/api/planner/v1/schema/?format=json').json()

    assert sorted(schema['paths']) == [
        f'/api/planner/v1/{path}'
        for path in (
            'agencies/',
            'alerts/',
            'plan/',
            'route/',
            'routes/',
            'status/',
            'stop/',
            'stop/departures/',
            'stop/timetable/',
            'stops/',
            'stops/nearby/',
            'stops/search/',
            'trip/',
            'trip/block/',
            'vehicles/',
        )
    ]
    plan = schema['paths']['/api/planner/v1/plan/']['get']['parameters']
    required = {p['name'] for p in plan if p.get('required')}
    assert required == {'from', 'to'}
    assert schema['components']['securitySchemes']['ApiKey']['name'] == 'X-API-Key'


def test_public_schema_generates_without_warnings():
    # Warnings (e.g. two components with one name) mean a wrong published contract.
    call_command(
        'spectacular',
        '--urlconf',
        'planner.api.schema_urls',
        '--validate',
        '--fail-on-warn',
        stdout=io.StringIO(),
    )


def test_ipv4_clients_seen_through_a_dual_stack_socket_stay_ipv4(enabled):
    IpBan.objects.create(network='203.0.113.0/24')
    PlannerSettings.objects.filter(pk=1).update(anonymous_lookup_per_minute=1)
    access.forget_cached_access()

    banned = get(LOOKUP, ip='::ffff:203.0.113.50')
    first = get(LOOKUP, ip='::ffff:198.51.100.1')
    other_client = get(LOOKUP, ip='::ffff:198.51.100.2')

    assert banned.status_code == 403
    assert (first.status_code, other_client.status_code) == (200, 200)


def test_banned_client_sending_a_key_still_gets_banned(enabled):
    IpBan.objects.create(network='203.0.113.50/32')

    response = get(LOOKUP, ip='203.0.113.50', **{'X-API-Key': 'odt_anything'})

    assert response.status_code == 403


def test_guessing_keys_is_cut_off_and_logged_once_a_minute(enabled):
    responses = [get(LOOKUP, **{'X-API-Key': f'odt_guess_{n}'}) for n in range(12)]

    assert [r.status_code for r in responses[:10]] == [401] * 10
    assert responses[10].json()['code'] == 'planner.rate_limited'
    assert PlannerRequestLog.objects.filter(status_code=401).count() == 1


def test_key_limit_of_zero_blocks_that_group(enabled):
    make_key('odt_one', lookup_per_minute=0)

    assert get(LOOKUP, **{'X-API-Key': 'odt_one'}).status_code == 429


def test_requests_while_switched_off_are_logged_once_a_minute():
    for _ in range(3):
        get(LOOKUP)

    assert PlannerRequestLog.objects.filter(status_code=503).count() == 1
