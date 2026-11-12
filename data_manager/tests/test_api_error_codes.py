"""Feed API errors are codes, not prose (ADR 0006; docs/api/README.md#error-codes)."""

from datetime import time

import pytest
from django.core.files.base import ContentFile

from cases.models import TransportOrganization
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    StaticFeedEntry,
)
from OtwarteDaneTransportowe.auth_roles import (
    ROLE_ADMIN,
    ROLE_BLOGGER,
    ROLE_DATA_PROVIDER,
    ROLE_HELPER,
)

pytestmark = pytest.mark.django_db

API = '/api/data_manager'
PRIVATE_HOST = 'intranet.example.org'


def _request_error(code):
    return {'code': code, 'details': {}}


@pytest.fixture(autouse=True)
def _private_host(fake_dns):
    fake_dns[PRIVATE_HOST] = '10.0.0.5'


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def owner(make_user):
    return make_user(ROLE_DATA_PROVIDER, 'owner')


@pytest.fixture
def owner_client(owner, client_as):
    return client_as(owner)


def _static(org, owner, *, stage_after=2, data_type='gtfs'):
    """A static submission with a URL source; stage 2 (in review) or 4 (published)."""
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type=data_type, name='Static'
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=(
            FeedSubmissionHistory.EVENT_COMPLETED
            if stage_after == 4
            else FeedSubmissionHistory.EVENT_UPLOADED
        ),
        stage_before=stage_after - 1,
        stage_after=stage_after,
        actor=owner,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.org/gtfs.zip',
        hide_original=True,
        download_time_1=time(3, 0),
    )
    return submission


@pytest.fixture
def static_submission(org, owner):
    return _static(org, owner)


def _gbfs_payload(org, **overrides):
    payload = {
        'transport_organization': org.id,
        'protocol': 'gbfs',
        'name': 'Bikes',
        'endpoints': [
            {'endpoint_type': 'gbfs', 'url': 'https://example.org/gbfs.json', 'interval': 60}
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Request errors: {code, details}
# ---------------------------------------------------------------------------


def test_download_without_feed_id(client):
    response = client.get('/feed/')

    assert response.status_code == 400
    assert response.json() == _request_error('feeds.feed_id_required')


def test_user_submissions_without_feed_role(make_user, client_as):
    response = client_as(make_user(ROLE_BLOGGER)).get(f'{API}/submissions/')

    assert response.status_code == 403
    assert response.data == _request_error('feeds.submissions_forbidden')


def test_owner_cannot_set_stage(owner_client, static_submission):
    response = owner_client.patch(
        f'{API}/feed-submissions/{static_submission.id}/', {'stage': 3}, format='json'
    )

    assert response.status_code == 403
    assert response.data == _request_error('feeds.review_forbidden')


def test_helper_cannot_edit_content(make_user, client_as, static_submission):
    response = client_as(make_user(ROLE_HELPER)).patch(
        f'{API}/feed-submissions/{static_submission.id}/', {'note': 'x'}, format='json'
    )

    assert response.status_code == 403
    assert response.data == _request_error('feeds.helper_content_edit_forbidden')


def test_owner_cannot_delete(owner_client, static_submission):
    response = owner_client.delete(f'{API}/feed-submissions/{static_submission.id}/')

    assert response.status_code == 403
    assert response.data == _request_error('feeds.delete_forbidden')


def test_owner_cannot_pause_fetching(owner_client, static_submission):
    entry = static_submission.static_entry

    response = owner_client.post(f'{API}/static-feed-entries/{entry.id}/pause-fetch/')

    assert response.status_code == 403
    assert response.data == _request_error('feeds.fetch_control_forbidden')


# ---------------------------------------------------------------------------
# Field errors: the code is the message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('stage', ['two', 7])
def test_invalid_stage(make_user, client_as, static_submission, stage):
    response = client_as(make_user(ROLE_HELPER)).patch(
        f'{API}/feed-submissions/{static_submission.id}/', {'stage': stage}, format='json'
    )

    assert response.status_code == 400
    assert response.data == {'stage': ['feeds.invalid_stage']}


@pytest.mark.parametrize(
    'body, expected',
    [
        ({'name': 'Renamed'}, {'name': ['feeds.field_locked']}),
        ({'data_type': 'netex'}, {'data_type': ['feeds.field_locked']}),
        (
            {'static_entry': {'url': 'https://example.org/other.zip', 'is_original': False}},
            {
                'static_entry': {
                    'is_original': ['feeds.field_locked'],
                    'url': ['feeds.field_locked'],
                }
            },
        ),
    ],
)
def test_static_fields_locked_in_review(owner_client, static_submission, body, expected):
    response = owner_client.patch(
        f'{API}/feed-submissions/{static_submission.id}/', body, format='json'
    )

    assert response.status_code == 400
    assert response.data == expected


def _create_static(client, org, static_entry, *, data_type='gtfs', format='json'):
    if format == 'multipart':
        body = {'transport_organization': org.id, 'data_type': data_type, 'name': 'New'}
        body.update({f'static_entry.{key}': value for key, value in static_entry.items()})
    else:
        body = {
            'transport_organization': org.id,
            'data_type': data_type,
            'name': 'New',
            'static_entry': static_entry,
        }
    return client.post(f'{API}/feed-submissions/', body, format=format)


def test_blocked_url_does_not_leak_the_reason(owner_client, org):
    response = _create_static(
        owner_client,
        org,
        {'url': f'https://{PRIVATE_HOST}/gtfs.zip', 'download_time_1': '03:00:00'},
    )

    assert response.status_code == 400
    assert response.data == {'static_entry': {'url': ['feeds.url_blocked']}}


def test_static_source_missing(owner_client, org):
    response = _create_static(owner_client, org, {'license': 'CC BY 4.0'})

    assert response.status_code == 400
    assert response.data == {'static_entry': ['feeds.static_source_missing']}


def test_url_source_needs_download_time(owner_client, org):
    response = _create_static(owner_client, org, {'url': 'https://example.org/gtfs.zip'})

    assert response.status_code == 400
    assert response.data == {'static_entry': {'download_time_1': ['feeds.download_time_required']}}


def test_url_and_file_together(owner_client, org):
    response = _create_static(
        owner_client,
        org,
        {
            'url': 'https://example.org/gtfs.zip',
            'download_time_1': '03:00:00',
            'file': ContentFile(b'PK\x03\x04rest', name='gtfs.zip'),
        },
        format='multipart',
    )

    assert response.status_code == 400
    assert response.data == {'static_entry': {'__all__': ['feeds.static_source_url_and_file']}}


def test_uploaded_gtfs_must_be_a_zip(owner_client, org):
    response = _create_static(
        owner_client,
        org,
        {'file': ContentFile(b'not a zip', name='gtfs.zip')},
        format='multipart',
    )

    assert response.status_code == 400
    assert response.data == {'static_entry': {'file': ['feeds.gtfs_not_zip']}}


def test_realtime_endpoint_url_blocked(owner_client, org):
    endpoints = [
        {'endpoint_type': 'gbfs', 'url': f'https://{PRIVATE_HOST}/gbfs.json', 'interval': 60}
    ]

    response = owner_client.post(
        f'{API}/realtime-submissions/', _gbfs_payload(org, endpoints=endpoints), format='json'
    )

    assert response.status_code == 400
    assert response.data == {'endpoints': {0: {'url': ['feeds.url_blocked']}}}


@pytest.mark.parametrize(
    'overrides, expected',
    [
        ({'endpoints': []}, {'endpoints': ['realtime.endpoints_required']}),
        (
            {
                'endpoints': [
                    {'endpoint_type': 'gbfs', 'url': 'https://example.org/a.json'},
                    {'endpoint_type': 'gbfs', 'url': 'https://example.org/b.json'},
                ]
            },
            {'endpoints': ['realtime.endpoint_type_duplicate']},
        ),
        (
            {'endpoints': [{'endpoint_type': 'trip_update', 'url': 'https://example.org/rt'}]},
            {'endpoints': ['realtime.endpoints_invalid_for_protocol']},
        ),
        ({'protocol': 'gtfs_rt'}, {'static_submission': ['realtime.static_submission_required']}),
    ],
)
def test_realtime_create_rules(owner_client, org, overrides, expected):
    response = owner_client.post(
        f'{API}/realtime-submissions/', _gbfs_payload(org, **overrides), format='json'
    )

    assert response.status_code == 400
    assert response.data == expected


@pytest.mark.parametrize(
    'case, code',
    [
        ('gbfs', 'realtime.static_submission_not_allowed'),
        ('not_published', 'realtime.static_submission_not_published'),
        ('netex', 'realtime.static_submission_type_mismatch'),
        ('other_org', 'realtime.static_submission_other_organization'),
        ('not_owned', 'realtime.static_submission_not_owned'),
    ],
)
def test_realtime_static_submission_rules(owner_client, owner, make_user, org, case, code):
    other_org = TransportOrganization.objects.create(region='R', transport_organization='Other')
    static = {
        'gbfs': lambda: _static(org, owner, stage_after=4),
        'not_published': lambda: _static(org, owner, stage_after=2),
        'netex': lambda: _static(org, owner, stage_after=4, data_type='netex'),
        'other_org': lambda: _static(other_org, owner, stage_after=4),
        'not_owned': lambda: _static(org, make_user(ROLE_DATA_PROVIDER), stage_after=4),
    }[case]()
    payload = _gbfs_payload(
        org,
        protocol='gbfs' if case == 'gbfs' else 'gtfs_rt',
        static_submission=static.id,
        endpoints=[{'endpoint_type': 'trip_update', 'url': 'https://example.org/rt'}]
        if case != 'gbfs'
        else _gbfs_payload(org)['endpoints'],
    )

    response = owner_client.post(f'{API}/realtime-submissions/', payload, format='json')

    assert response.status_code == 400
    assert response.data == {'static_submission': [code]}


@pytest.fixture
def realtime_in_review(org, owner):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=owner,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='Bikes',
    )
    rts.history.create(event_type='uploaded', stage_before=1, stage_after=2, actor=owner)
    RealtimeEndpointRT.objects.create(
        submission=rts, endpoint_type='gbfs', url='https://example.org/gbfs.json', interval=60
    )
    return rts


@pytest.mark.parametrize(
    'body, expected',
    [
        ({'name': 'Renamed'}, {'name': ['feeds.field_locked']}),
        (
            {'endpoints': [{'endpoint_type': 'gbfs', 'url': 'https://example.org/new.json'}]},
            {'endpoints': ['feeds.field_locked']},
        ),
        (
            {
                'endpoints': [
                    {'endpoint_type': 'gbfs', 'interval': 60},
                    {'endpoint_type': 'gbfs_versions', 'url': 'https://example.org/v.json'},
                ]
            },
            {'endpoints': ['realtime.endpoints_locked']},
        ),
        (
            {'endpoints': [{'endpoint_type': 'system_information', 'interval': 60}]},
            {'endpoints': ['realtime.endpoint_type_unknown']},
        ),
    ],
)
def test_realtime_fields_locked_in_review(owner_client, realtime_in_review, body, expected):
    response = owner_client.patch(
        f'{API}/realtime-submissions/{realtime_in_review.id}/', body, format='json'
    )

    assert response.status_code == 400
    assert response.data == expected


def test_admin_override_still_gets_codes(make_user, client_as, static_submission):
    """Admin may edit at any stage, but invalid content is still coded."""
    response = client_as(make_user(ROLE_ADMIN)).patch(
        f'{API}/feed-submissions/{static_submission.id}/',
        {'static_entry': {'url': f'https://{PRIVATE_HOST}/gtfs.zip'}},
        format='json',
    )

    assert response.status_code == 400
    assert response.data == {'static_entry': {'url': ['feeds.url_blocked']}}
