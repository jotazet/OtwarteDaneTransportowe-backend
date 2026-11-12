"""Realtime submissions link only to the caller's own static feed.

A DataProvider may link GTFS-RT/SIRI only to a static submission it submitted
itself, as `eligible-static-submissions` lists (docs/api/feeds.md); Admin may
link any. A provider's view of its static feed and its realtime downloads show
only its own realtime submissions.
"""

import pytest
from django.core.files.base import ContentFile

from cases.models import TransportOrganization
from data_manager.models import RealtimeEndpointRT, RealtimeSubmission
from data_manager.tests.test_realtime_create import (
    URL,
    _gtfs_rt_payload,
    _static_submission,
)
from OtwarteDaneTransportowe.auth_roles import ROLE_ADMIN, ROLE_DATA_PROVIDER, ROLE_HELPER

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('fake_dns')]

NOT_OWNED = 'realtime.static_submission_not_owned'


@pytest.fixture
def org():
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def owner(make_user):
    return make_user(ROLE_DATA_PROVIDER, 'owner')


@pytest.fixture
def other(make_user):
    return make_user(ROLE_DATA_PROVIDER, 'other')


@pytest.fixture
def static(org, owner):
    return _static_submission(org, owner)


def _cached_endpoint(org, static, user, protocol):
    """A realtime submission with one cached hide_original endpoint.

    Created directly, bypassing the API's rules, as a link made before the
    ownership check existed; the unique (static, protocol) slot needs a
    different protocol per submission.
    """
    submission = RealtimeSubmission.objects.create(
        transport_organization=org,
        static_submission=static,
        submitted_by=user,
        protocol=protocol,
        name='Realtime',
    )
    endpoint = RealtimeEndpointRT.objects.create(
        submission=submission,
        endpoint_type='trip_update',
        url='https://example.org/hidden.pb',
        hide_original=True,
    )
    endpoint.cached_file.save('feed.pb', ContentFile(b'cached'), save=True)
    return endpoint


@pytest.fixture
def linked(settings, tmp_path, org, owner, other, static):
    """The owner's realtime endpoint and another provider's, on the owner's static feed."""
    settings.MEDIA_ROOT = tmp_path
    return (
        _cached_endpoint(org, static, owner, RealtimeSubmission.PROTOCOL_GTFS_RT),
        _cached_endpoint(org, static, other, RealtimeSubmission.PROTOCOL_SIRI),
    )


def test_provider_links_realtime_to_own_static(org, owner, static, client_as):
    response = client_as(owner).post(URL, _gtfs_rt_payload(org, static), format='json')

    assert response.status_code == 201, response.data


def test_provider_cannot_link_realtime_to_another_providers_static(org, other, static, client_as):
    response = client_as(other).post(URL, _gtfs_rt_payload(org, static), format='json')

    assert response.status_code == 400
    assert response.data['static_submission'][0].code == NOT_OWNED
    assert not RealtimeSubmission.objects.filter(static_submission=static).exists()


def test_provider_cannot_move_realtime_to_another_providers_static(
    org, owner, other, static, client_as
):
    own_static = _static_submission(org, other)
    created = client_as(other).post(URL, _gtfs_rt_payload(org, own_static), format='json')
    assert created.status_code == 201, created.data

    response = client_as(other).patch(
        f"{URL}{created.data['id']}/", {'static_submission': static.id}, format='json'
    )

    assert response.status_code == 400
    assert response.data['static_submission'][0].code == NOT_OWNED
    assert RealtimeSubmission.objects.get(pk=created.data['id']).static_submission == own_static


def test_admin_links_realtime_to_any_static(org, static, make_user, client_as):
    admin = make_user(ROLE_ADMIN)

    response = client_as(admin).post(URL, _gtfs_rt_payload(org, static), format='json')

    assert response.status_code == 201, response.data


def _realtime_ids(response):
    return sorted(rt['id'] for rt in response.data['realtime_submissions'])


def _detail(static):
    return f'/api/data_manager/feed-submissions/{static.id}/'


def _download(static, endpoint):
    return f'{_detail(static)}download/realtime/{endpoint.id}/'


def test_static_feed_owner_sees_only_own_realtime(owner, static, linked, client_as):
    own, _ = linked

    response = client_as(owner).get(_detail(static))

    assert response.status_code == 200
    assert _realtime_ids(response) == [own.submission_id]


@pytest.mark.parametrize('role', [ROLE_HELPER, ROLE_ADMIN])
def test_reviewer_sees_all_realtime_of_static_feed(role, static, linked, make_user, client_as):
    response = client_as(make_user(role)).get(_detail(static))

    assert response.status_code == 200
    assert _realtime_ids(response) == sorted(e.submission_id for e in linked)


def test_static_feed_owner_downloads_only_own_realtime_cache(owner, static, linked, client_as):
    own, foreign = linked
    client = client_as(owner)

    assert client.get(_download(static, own)).status_code == 200
    assert client.get(_download(static, foreign)).status_code == 404


@pytest.mark.parametrize('role', [ROLE_HELPER, ROLE_ADMIN])
def test_reviewer_downloads_any_realtime_cache(role, static, linked, make_user, client_as):
    client = client_as(make_user(role))

    for endpoint in linked:
        assert client.get(_download(static, endpoint)).status_code == 200
