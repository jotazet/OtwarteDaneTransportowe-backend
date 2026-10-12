"""Admin API of the public access layer, and the request log retention."""

from datetime import timedelta

import pytest
from django.utils import timezone

from planner import access, request_logs
from planner.models import ApiKey, IpBan, PlannerDailyStat, PlannerRequestLog, PlannerSettings

pytestmark = pytest.mark.django_db

BASE = '/api/planner/admin'


@pytest.fixture
def admin_client(make_user, client_as):
    return client_as(make_user('Admin'))


@pytest.mark.parametrize(
    ('role', 'expected'),
    [(None, 401), ('Helper', 403), ('DataProvider', 403), ('Editor', 403), ('Admin', 200)],
)
@pytest.mark.parametrize(
    'path',
    ['/settings/', '/api-keys/', '/ip-bans/', '/requests/', '/stats/daily/', '/stats/top-clients/'],
)
def test_access_administration_requires_the_admin_group(make_user, client_as, role, expected, path):
    assert client_as(make_user(role)).get(BASE + path).status_code == expected


def test_settings_change_applies_at_once(admin_client):
    access.planner_settings()  # cached

    response = admin_client.patch(
        f'{BASE}/settings/', {'public_api_enabled': True, 'key_plan_per_minute': 50}, format='json'
    )

    assert response.data['public_api_enabled'] is True
    assert access.planner_settings().key_plan_per_minute == 50
    assert PlannerSettings.load().updated_by is not None


def test_new_key_is_shown_once_and_stored_hashed(admin_client):
    created = admin_client.post(f'{BASE}/api-keys/', {'name': 'Gmina app'}, format='json')
    listed = admin_client.get(f'{BASE}/api-keys/')

    raw = created.data['key']
    key = ApiKey.objects.get()
    assert raw.startswith('odt_') and created.status_code == 201
    assert key.key_hash == access.hash_key(raw) and raw not in key.key_hash
    assert created.data['prefix'] == raw[:12]
    assert 'key' not in listed.data['results'][0]


def test_key_is_revoked_not_deleted(admin_client):
    key = ApiKey.objects.create(name='x', prefix='odt_x', key_hash='h')

    revoked = admin_client.patch(f'{BASE}/api-keys/{key.id}/', {'is_active': False}, format='json')
    deleted = admin_client.delete(f'{BASE}/api-keys/{key.id}/')

    assert revoked.data['is_active'] is False
    assert deleted.status_code == 405


def test_ban_networks_are_normalised_validated_and_applied_at_once(admin_client):
    assert access.active_bans() == []

    created = admin_client.post(f'{BASE}/ip-bans/', {'network': '203.0.113.7'}, format='json')
    invalid = admin_client.post(f'{BASE}/ip-bans/', {'network': 'not-an-ip'}, format='json')

    assert created.data['network'] == '203.0.113.7/32'
    assert invalid.data['network'][0].code == 'planner.invalid_network'
    assert access.is_banned('203.0.113.7')


def test_request_log_is_filtered(admin_client):
    now = timezone.now()
    PlannerRequestLog.objects.create(
        endpoint='a', status_code=200, latency_ms=5, client_ip='198.51.100.1'
    )
    PlannerRequestLog.objects.create(
        endpoint='a', status_code=429, latency_ms=1, client_ip='198.51.100.2'
    )
    PlannerRequestLog.objects.create(
        endpoint='a',
        status_code=200,
        latency_ms=5,
        client_ip='198.51.100.1',
        created_at=now - timedelta(days=3),
    )

    recent = admin_client.get(f'{BASE}/requests/')
    by_ip = admin_client.get(f'{BASE}/requests/?ip=198.51.100.1')
    older = admin_client.get(
        f'{BASE}/requests/', {'since': (now - timedelta(days=5)).isoformat(), 'status': 200}
    )

    assert recent.data['count'] == 2
    assert by_ip.data['count'] == 1
    assert older.data['count'] == 2


def test_daily_stats_combine_rollups_and_today(admin_client):
    yesterday = timezone.now().date() - timedelta(days=1)
    PlannerDailyStat.objects.create(
        date=yesterday,
        endpoint='a',
        status_code=200,
        requests=10,
        latency_ms_sum=100,
        latency_ms_max=30,
    )
    PlannerDailyStat.objects.create(
        date=yesterday,
        endpoint='a',
        status_code=429,
        requests=2,
        latency_ms_sum=2,
        latency_ms_max=1,
    )
    PlannerRequestLog.objects.create(endpoint='a', status_code=500, latency_ms=40)

    days = admin_client.get(f'{BASE}/stats/daily/').data['days']

    assert days[0] | {'date': None} == {
        'date': None,
        'requests': 12,
        'ok': 10,
        'client_errors': 0,
        'rate_limited': 2,
        'server_errors': 0,
        'max_latency_ms': 30,
        'avg_latency_ms': 8,
    }
    assert days[1]['server_errors'] == 1


def test_top_clients_name_keys_and_ips(admin_client):
    key = ApiKey.objects.create(name='Gmina app', prefix='odt_abc', key_hash='h')
    for _ in range(3):
        PlannerRequestLog.objects.create(
            endpoint='a', status_code=200, latency_ms=1, api_key=key, client_ip='198.51.100.9'
        )
    PlannerRequestLog.objects.create(
        endpoint='a', status_code=200, latency_ms=1, client_ip='198.51.100.1'
    )

    clients = admin_client.get(f'{BASE}/stats/top-clients/?days=7').data['clients']

    assert clients[0]['api_key'] == {'id': key.id, 'name': 'Gmina app', 'prefix': 'odt_abc'}
    assert clients[0]['requests'] == 3


def test_logs_are_summarised_without_ips_then_purged_after_retention(settings):
    settings.PLANNER_REQUEST_LOG_RETENTION_DAYS = 30
    now = timezone.now()
    for days_ago in (31, 1, 1, 0):
        PlannerRequestLog.objects.create(
            endpoint='a',
            status_code=200,
            latency_ms=10,
            client_ip='198.51.100.1',
            created_at=now - timedelta(days=days_ago),
        )

    request_logs.roll_up_and_purge()
    request_logs.roll_up_and_purge()  # idempotent

    stats = {s.date: s.requests for s in PlannerDailyStat.objects.all()}
    assert stats == {(now - timedelta(days=31)).date(): 1, (now - timedelta(days=1)).date(): 2}
    assert PlannerRequestLog.objects.count() == 3
    assert not hasattr(PlannerDailyStat, 'client_ip')


def test_admin_lists_do_not_query_per_row(admin_client, django_assert_max_num_queries):
    for n in range(10):
        key = ApiKey.objects.create(name=f'k{n}', prefix=f'odt_{n}', key_hash=f'h{n}')
        IpBan.objects.create(network=f'198.51.100.{n}/32')
        PlannerRequestLog.objects.create(endpoint='a', status_code=200, latency_ms=1, api_key=key)

    for path in ('/api-keys/', '/ip-bans/', '/requests/'):
        with django_assert_max_num_queries(8):
            assert admin_client.get(BASE + path).data['count'] == 10
