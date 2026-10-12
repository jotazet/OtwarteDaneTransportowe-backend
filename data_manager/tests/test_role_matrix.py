"""Who may do what on the feed endpoints, for every role (docs/api/feeds.md,
"Who sees what" in docs/domain/feed-workflow.md).

Actors: anonymous; the owner (DataProvider who submitted the feeds); another
DataProvider; Helper; Blogger; Editor; Admin (the group, not a superuser).
Blogger and Editor hold no feed role, so the feed endpoints treat them like
any other logged-in user without one.
"""

import socket
from datetime import time

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager.models import (
    FETCH_STATUS_MANUAL_PAUSED,
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)
from OtwarteDaneTransportowe.auth_roles import (
    ROLE_ADMIN,
    ROLE_BLOGGER,
    ROLE_DATA_PROVIDER,
    ROLE_EDITOR,
    ROLE_HELPER,
)

pytestmark = pytest.mark.django_db

API = '/api/data_manager'
ROLE_OF = {
    'owner': ROLE_DATA_PROVIDER,
    'other_provider': ROLE_DATA_PROVIDER,
    'helper': ROLE_HELPER,
    'blogger': ROLE_BLOGGER,
    'editor': ROLE_EDITOR,
    'admin': ROLE_ADMIN,
}
SUBMISSION_KINDS = ['feed-submissions', 'realtime-submissions']


def _user(username, role):
    user = get_user_model().objects.create_user(username, f'{username}@example.com', 'password')
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Source URLs are SSRF-checked on input; resolve them without real DNS."""
    monkeypatch.setattr(
        'data_manager.net_security.socket.getaddrinfo',
        lambda host, port, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))
        ],
    )


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def owner():
    return _user('owner', ROLE_DATA_PROVIDER)


@pytest.fixture
def client_for(owner):
    """APIClient for an actor name; 'owner' is the fixture user, others are fresh."""

    def make(actor):
        client = APIClient()
        if actor == 'owner':
            client.force_authenticate(user=owner)
        elif actor != 'anonymous':
            client.force_authenticate(user=_user(actor, ROLE_OF[actor]))
        return client

    return make


@pytest.fixture
def static_submission(org, owner):
    """The owner's proxied static feed at stage 2, with one fetch error."""
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type='gtfs', name='Static'
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=2,
        actor=owner,
    )
    entry = StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1=time(3, 0),
    )
    FeedFetchError.objects.create(
        static_entry=entry,
        error_type=FeedFetchError.ERROR_TIMEOUT,
        message='timed out',
        url_attempted=entry.url,
    )
    return submission


@pytest.fixture
def realtime_submission(org, owner):
    """The owner's proxied GBFS feed at stage 1, with one fetch error."""
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=owner,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='Bikes',
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
        actor=owner,
    )
    endpoint = RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.org/gbfs.json',
        hide_original=True,
        interval=60,
    )
    FeedFetchError.objects.create(
        endpoint_rt=endpoint,
        error_type=FeedFetchError.ERROR_HTTP,
        http_status_code=503,
        message='unavailable',
        url_attempted=endpoint.url,
    )
    return rts


@pytest.fixture
def submissions(static_submission, realtime_submission):
    return {'feed-submissions': static_submission, 'realtime-submissions': realtime_submission}


# ---------------------------------------------------------------------------
# Static and realtime submissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('kind', SUBMISSION_KINDS)
@pytest.mark.parametrize(
    'actor, expected, sees_it',
    [
        ('anonymous', 401, False),
        ('blogger', 403, False),
        ('editor', 403, False),
        ('other_provider', 200, False),
        ('owner', 200, True),
        ('helper', 200, True),
        ('admin', 200, True),
    ],
)
def test_list_by_role(kind, actor, expected, sees_it, submissions, client_for):
    response = client_for(actor).get(f'{API}/{kind}/')

    assert response.status_code == expected
    if expected == 200:
        ids = [row['id'] for row in response.data['results']]
        assert ids == ([submissions[kind].id] if sees_it else [])


@pytest.mark.parametrize('kind', SUBMISSION_KINDS)
@pytest.mark.parametrize(
    'actor, expected',
    [
        ('anonymous', 401),
        ('blogger', 403),
        ('editor', 403),
        # Someone else's submission is outside the provider's queryset.
        ('other_provider', 404),
        ('owner', 200),
        ('helper', 200),
        ('admin', 200),
    ],
)
def test_detail_by_role(kind, actor, expected, submissions, client_for):
    submission = submissions[kind]

    response = client_for(actor).get(f'{API}/{kind}/{submission.id}/')

    assert response.status_code == expected
    if expected == 200:
        assert response.data['id'] == submission.id


@pytest.mark.parametrize('kind', SUBMISSION_KINDS)
@pytest.mark.parametrize(
    'actor, expected',
    [
        ('anonymous', 401),
        ('blogger', 403),
        ('editor', 403),
        ('other_provider', 404),
        ('owner', 403),
        ('helper', 200),
        ('admin', 200),
    ],
)
def test_confirm_stage_by_role(kind, actor, expected, submissions, client_for):
    submission = submissions[kind]
    stage_before = submission.current_stage

    response = client_for(actor).patch(
        f'{API}/{kind}/{submission.id}/', {'stage': 3}, format='json'
    )

    assert response.status_code == expected
    submission = type(submission).objects.get(pk=submission.pk)
    assert submission.current_stage == (3 if expected == 200 else stage_before)


@pytest.mark.parametrize('kind', SUBMISSION_KINDS)
@pytest.mark.parametrize(
    'actor, expected',
    [
        ('anonymous', 401),
        ('blogger', 403),
        ('editor', 403),
        ('other_provider', 403),
        ('owner', 403),
        ('helper', 403),
        # The Admin group alone, without is_superuser.
        ('admin', 204),
    ],
)
def test_delete_by_role(kind, actor, expected, submissions, client_for):
    submission = submissions[kind]

    response = client_for(actor).delete(f'{API}/{kind}/{submission.id}/')

    assert response.status_code == expected
    assert type(submission).objects.filter(pk=submission.pk).exists() is (expected != 204)


@pytest.mark.parametrize(
    'actor, expected',
    [
        ('anonymous', 401),
        ('blogger', 403),
        ('editor', 403),
        ('helper', 403),
        ('owner', 201),
        ('admin', 201),
    ],
)
def test_static_create_by_role(actor, expected, org, client_for):
    response = client_for(actor).post(
        f'{API}/feed-submissions/',
        {
            'transport_organization': org.id,
            'data_type': 'gtfs',
            'name': 'New feed',
            'static_entry': {
                'url': 'https://example.org/new.zip',
                'download_time_1': '03:00:00',
                'license': 'CC BY 4.0',
            },
        },
        format='json',
    )

    assert response.status_code == expected, response.data
    if expected == 201:
        assert response.data['current_stage'] == 2
        assert [h['event_type'] for h in response.data['history']] == ['uploaded']
    else:
        assert not FeedSubmission.objects.exists()


# ---------------------------------------------------------------------------
# Combined, operational and public lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'actor, expected, own',
    [
        ('anonymous', 401, False),
        ('blogger', 403, False),
        ('editor', 403, False),
        ('other_provider', 200, False),
        ('owner', 200, True),
        # Own submissions only, whatever the role.
        ('helper', 200, False),
        ('admin', 200, False),
    ],
)
def test_user_submissions_by_role(actor, expected, own, submissions, client_for):
    response = client_for(actor).get(f'{API}/submissions/')

    assert response.status_code == expected
    if expected == 200:
        assert [row['id'] for row in response.data['static']] == (
            [submissions['feed-submissions'].id] if own else []
        )
        assert [row['id'] for row in response.data['realtime']] == (
            [submissions['realtime-submissions'].id] if own else []
        )


@pytest.mark.parametrize(
    'path, count',
    [
        ('proxy-feeds/', 2),
        ('fetch-errors/', 2),
        ('static-feed-entries/', 1),
        ('realtime-endpoints/', 1),
    ],
)
@pytest.mark.parametrize(
    'actor, expected, sees_all',
    [
        ('anonymous', 401, False),
        # Any logged-in user may call these; without a feed role they see nothing.
        ('blogger', 200, False),
        ('editor', 200, False),
        ('other_provider', 200, False),
        ('owner', 200, True),
        ('helper', 200, True),
        ('admin', 200, True),
    ],
)
def test_operational_lists_by_role(path, count, actor, expected, sees_all, submissions, client_for):
    response = client_for(actor).get(f'{API}/{path}')

    assert response.status_code == expected
    if expected == 200:
        assert response.data['count'] == (count if sees_all else 0)


@pytest.fixture
def proxied(static_submission, realtime_submission):
    return {
        'static-feed-entries': static_submission.static_entry,
        'realtime-endpoints': realtime_submission.endpoints.get(),
    }


@pytest.mark.parametrize('resource', ['static-feed-entries', 'realtime-endpoints'])
@pytest.mark.parametrize(
    'actor, expected',
    [
        ('anonymous', 401),
        ('blogger', 403),
        ('editor', 403),
        ('other_provider', 403),
        ('owner', 403),
        ('helper', 200),
        ('admin', 200),
    ],
)
def test_pause_fetch_by_role(resource, actor, expected, proxied, client_for):
    feed = proxied[resource]

    response = client_for(actor).post(f'{API}/{resource}/{feed.id}/pause-fetch/')

    assert response.status_code == expected
    feed.refresh_from_db()
    assert (feed.fetch_status == FETCH_STATUS_MANUAL_PAUSED) is (expected == 200)


@pytest.mark.parametrize('actor', ['anonymous', 'blogger', 'editor'])
def test_public_catalogue_is_open_to_everyone(actor, org, owner, client_for):
    published = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type='gtfs', name='Published'
    )
    FeedSubmissionHistory.objects.create(
        submission=published,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=owner,
    )
    client = client_for(actor)

    response = client.get(f'{API}/feeds/')
    assert response.status_code == 200
    assert [row['id'] for row in response.data['results']] == [org.id]

    response = client.get(f'{API}/feeds/{org.id}/')
    assert response.status_code == 200
    assert [feed['id'] for feed in response.data['feeds']] == [published.id]
