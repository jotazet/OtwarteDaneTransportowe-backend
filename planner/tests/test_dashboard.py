"""Admin dashboard statistics over the raw request log (charts, endpoints, clients)."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from planner.models import ApiKey, IpBan, PlannerRequestLog

pytestmark = pytest.mark.django_db

BASE = '/api/planner/admin/stats'
T0 = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


@pytest.fixture
def admin_client(make_user, client_as):
    return client_as(make_user('Admin'))


def log(
    minutes, *, endpoint='planner-v1-plan', status=200, latency=100, ip='198.51.100.1', **extra
):
    return PlannerRequestLog.objects.create(
        created_at=T0 + timedelta(minutes=minutes),
        endpoint=endpoint,
        status_code=status,
        latency_ms=latency,
        client_ip=ip,
        **extra,
    )


@pytest.fixture
def traffic():
    """Two hours of requests from three clients, one of them a scraper with a bot UA."""
    key = ApiKey.objects.create(name='Gmina app', prefix='odt_abc', key_hash='h')
    for minute in range(0, 120, 10):  # 12 plans from an app with a key
        log(minute, latency=200, ip='198.51.100.1', api_key=key, user_agent='GminaApp/1.0')
    for minute in range(0, 60, 2):  # 30 searches in the first hour from a scraper
        log(
            minute,
            endpoint='planner-v1-stop-search',
            latency=20,
            ip='203.0.113.9',
            user_agent='bot',
        )
    log(
        61,
        endpoint='planner-v1-stop-search',
        status=429,
        latency=1,
        ip='203.0.113.9',
        user_agent='bot',
    )
    log(70, status=503, latency=12000, ip='198.51.100.7', user_agent='Mozilla/5.0')
    log(80, status=400, latency=5, ip='198.51.100.7', user_agent='Mozilla/5.0')
    return key


def get(client, path, **params):
    params.setdefault('since', T0.isoformat())
    params.setdefault('until', (T0 + timedelta(hours=2)).isoformat())
    return client.get(f'{BASE}/{path}/', params)


def test_summary_tiles(admin_client, traffic):
    body = get(admin_client, 'summary').data

    assert body | {
        'since': None,
        'until': None,
        'avg_latency_ms': None,
        'p95_latency_ms': None,
    } == {
        'since': None,
        'until': None,
        'requests': 45,
        'unique_ips': 3,
        'unique_api_keys': 1,
        'ok': 42,
        'client_errors': 1,
        'rate_limited': 1,
        'server_errors': 1,
        'avg_latency_ms': None,
        'p95_latency_ms': None,
        'max_latency_ms': 12000,
    }
    assert body['p95_latency_ms'] == 200


def test_chart_by_status_class_with_empty_buckets_filled(admin_client, traffic):
    body = get(admin_client, 'timeseries', interval='hour').data

    assert body['buckets'] == ['2026-10-13T10:00:00Z', '2026-10-13T11:00:00Z']
    assert {s['name']: s['requests'] for s in body['series']} == {
        'ok': [36, 6],
        'client_errors': [0, 1],
        'rate_limited': [0, 1],
        'server_errors': [0, 1],
    }
    # 8 requests in the second hour, one of them 12 s: the p95 shows it, the mean less so.
    assert body['latency_ms']['p95'][1] > body['latency_ms']['p95'][0]


def test_chart_by_endpoint_puts_the_rest_in_other(admin_client, traffic, monkeypatch):
    from planner import request_logs

    monkeypatch.setattr(request_logs, 'SPLIT_ENDPOINTS', 1)

    body = get(admin_client, 'timeseries', interval='hour', split='endpoint').data

    assert [(s['name'], s['requests']) for s in body['series']] == [
        ('planner-v1-stop-search', [30, 1]),
        ('other', [6, 8]),
    ]


def test_chart_for_one_client_by_the_minute(admin_client, traffic):
    body = get(
        admin_client,
        'timeseries',
        interval='minute',
        split='none',
        ip='203.0.113.9',
        until=(T0 + timedelta(minutes=4)).isoformat(),
    ).data

    assert body['series'] == [{'name': 'all', 'requests': [1, 0, 1, 0]}]
    assert body['latency_ms']['avg'] == [20, None, 20, None]


def test_too_many_points_is_refused(admin_client):
    response = get(
        admin_client, 'timeseries', interval='minute', until=(T0 + timedelta(days=2)).isoformat()
    )

    assert response.status_code == 400
    assert response.data['interval'][0].code == 'planner.too_many_buckets'


def test_endpoints_busiest_first_with_share_and_latency(admin_client, traffic):
    rows = get(admin_client, 'endpoints').data['results']

    assert [(r['endpoint'], r['requests'], r['share'], r['rate_limited']) for r in rows] == [
        ('planner-v1-stop-search', 31, 0.6889, 1),
        ('planner-v1-plan', 14, 0.3111, 0),
    ]
    assert rows[1]['max_latency_ms'] == 12000


def test_busiest_ips_with_their_user_agent_and_ban(admin_client, traffic):
    IpBan.objects.create(network='203.0.113.0/24')

    rows = get(admin_client, 'top-clients').data['results']

    scraper = rows[0]
    assert scraper | {'first_seen': None, 'last_seen': None} == {
        'client_ip': '203.0.113.9',
        'requests': 31,
        'share': 0.6889,
        'ok': 30,
        'client_errors': 0,
        'rate_limited': 1,
        'server_errors': 0,
        'endpoints': 1,
        'api_keys': 0,
        'user_agent': 'bot',
        'banned': True,
        'first_seen': None,
        'last_seen': None,
    }
    assert [r['client_ip'] for r in rows] == ['203.0.113.9', '198.51.100.1', '198.51.100.7']
    assert rows[1]['banned'] is False


def test_user_agents_with_how_many_addresses_send_them(admin_client, traffic):
    rows = get(admin_client, 'user-agents').data['results']

    assert [(r['user_agent'], r['requests'], r['ips']) for r in rows] == [
        ('bot', 31, 1),
        ('GminaApp/1.0', 12, 1),
        ('Mozilla/5.0', 2, 1),
    ]


def test_the_default_range_is_the_last_day(admin_client):
    PlannerRequestLog.objects.create(endpoint='a', status_code=200, latency_ms=1)
    PlannerRequestLog.objects.create(
        endpoint='a', status_code=200, latency_ms=1, created_at=timezone.now() - timedelta(days=2)
    )

    assert admin_client.get(f'{BASE}/summary/').data['requests'] == 1


def test_an_inverted_range_is_refused(admin_client):
    response = get(admin_client, 'summary', since=(T0 + timedelta(hours=3)).isoformat())

    assert response.data['since'][0].code == 'planner.invalid_range'


@pytest.mark.parametrize(
    'path', ['summary', 'timeseries', 'endpoints', 'top-clients', 'user-agents']
)
@pytest.mark.parametrize(('role', 'expected'), [(None, 401), ('Helper', 403), ('Admin', 200)])
def test_only_admins_see_the_dashboard(make_user, client_as, path, role, expected):
    assert client_as(make_user(role)).get(f'{BASE}/{path}/').status_code == expected
