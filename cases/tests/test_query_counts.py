"""The organization list and detail run a fixed number of queries, however many rows they show."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cases.models import CaseStatus, DataProvider, TransportOrganization

pytestmark = pytest.mark.django_db

LIST_URL = '/api/cases/transport-organizations/'


def _query_count(url):
    with CaptureQueriesContext(connection) as ctx:
        response = APIClient().get(url)
    assert response.status_code == 200
    return len(ctx), response.data


def _add_rows(org, count):
    """Give ``org`` ``count`` more data providers and case statuses."""
    start = org.data_providers.count()
    for i in range(start, start + count):
        org.data_providers.add(DataProvider.objects.create(name=f'{org.pk}-{i}'))
        CaseStatus.objects.create(case=org, status='requested', description=f'{i}')


def _make_org(i):
    org = TransportOrganization.objects.create(region='R', transport_organization=f'Org {i}')
    _add_rows(org, 2)
    return org


def test_list_query_count_does_not_grow_with_organizations():
    _make_org(0)
    one, _ = _query_count(LIST_URL)
    for i in range(1, 5):
        _make_org(i)
    many, data = _query_count(LIST_URL)

    assert data['count'] == 5
    # count + page + data_providers prefetch + case_status prefetch
    assert one == many == 4


def test_detail_query_count_does_not_grow_with_related_rows():
    org = _make_org(0)
    url = f'{LIST_URL}{org.pk}/'
    one, _ = _query_count(url)
    _add_rows(org, 5)
    many, data = _query_count(url)

    assert len(data['data_providers']) == 7
    # the initial status from save() + 7 added ones
    assert len(data['statuses']) == 8
    # object + data_providers prefetch + case_status prefetch
    assert one == many == 3


def test_list_keeps_providers_by_name_and_latest_status():
    org = TransportOrganization.objects.create(region='R', transport_organization='Org')
    org.data_providers.add(
        *(DataProvider.objects.create(name=name) for name in ('Zeta', 'alpha', 'Beta'))
    )
    latest = CaseStatus.objects.create(case=org, status='received')

    row = APIClient().get(LIST_URL).data['results'][0]
    by_name = DataProvider.objects.order_by('name').values_list('name', flat=True)
    assert [p['name'] for p in row['data_providers']] == list(by_name)
    assert row['latest_status']['id'] == latest.id
