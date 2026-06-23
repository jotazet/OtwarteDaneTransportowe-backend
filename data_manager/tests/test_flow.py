import json
import pytest
import os
from datetime import time
from unittest.mock import patch

from django.core.files.base import ContentFile
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from rest_framework_simplejwt.tokens import AccessToken
from cases.models import TransportOrganization
from data_manager.models import (
    FeedSubmission,
    StaticFeedEntry,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
)
from django.test import override_settings
from django.conf import settings
from data_manager.tasks import validate_gtfs_feed_task

pytestmark = pytest.mark.django_db

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def admin_user():
    user = get_user_model()
    return user.objects.create_superuser('admin', 'admin@example.com', 'password')

@pytest.fixture
def normal_user():
    user = get_user_model()
    created = user.objects.create_user('user', 'user@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='DataProvider')
    created.groups.add(group)
    return created


@pytest.fixture
def helper_user():
    user = get_user_model().objects.create_user('helper', 'helper@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='Helper')
    user.groups.add(group)
    return user

@pytest.fixture
def org(normal_user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region="Test Region", transport_organization="Test Org")


def _mock_docker_from_env(error_count: int):
    """Simulate gtfs-validator container writing report.json (no Docker socket required)."""

    class _Containers:
        def run(self, image, command, volumes, **kwargs):
            output_host_dir = next(
                host for host, spec in volumes.items() if spec.get('bind') == '/output'
            )
            os.makedirs(output_host_dir, exist_ok=True)
            notices = []
            if error_count:
                notices.append({
                    'severity': 'ERROR',
                    'totalNotices': error_count,
                    'code': 'test_validation_error',
                })
            payload = {
                'summary': {'errorCount': error_count},
                'notices': notices,
            }
            with open(os.path.join(output_host_dir, 'report.json'), 'w', encoding='utf-8') as fh:
                json.dump(payload, fh)
            return b''

    class _DockerClient:
        containers = _Containers()

    return lambda: _DockerClient()


def test_gtfs_upload_flow_correct(api_client, admin_user, normal_user, org):
    # 1. User uploads GTFS
    api_client.force_authenticate(user=normal_user)

    gtfs_path = os.path.join(os.path.dirname(__file__), 'GTFS_correct.zip')

    upload_url = '/api/data_manager/feed-submissions/'
    payload = {
        'transport_organization': str(org.id),
        'data_type': 'gtfs',
        'name': 'Test GTFS Upload Correct',
        'static_entry.file': open(gtfs_path, 'rb'),
        'static_entry.is_original': False,
    }
    response = api_client.post(upload_url, payload, format='multipart')
    assert response.status_code == 201, response.data

    submission_id = response.data['id']
    submission = FeedSubmission.objects.get(id=submission_id)

    assert submission.current_stage == 2, f"Expected stage 2, got {submission.current_stage}"

    static_entry = StaticFeedEntry.objects.get(submission=submission)

    # 2. Run actual validation using Celery and Docker
    os.environ['HOST_MEDIA_ROOT'] = str(settings.MEDIA_ROOT)

    with override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True):
        with patch('docker.from_env', _mock_docker_from_env(0)):
            validate_gtfs_feed_task.delay(static_entry.id)
        static_entry.refresh_from_db()
        assert hasattr(static_entry, 'validation_report'), f"Validation report was not created for {static_entry.id}"
        assert static_entry.validation_report.error_count == 0, f"Expected no errors in the valid GTFS feed, but got {static_entry.validation_report.error_count}"
        assert static_entry.validation_status == StaticFeedEntry.VALIDATION_VALID

    submission.refresh_from_db()
    assert submission.current_stage == 3, f"Expected stage 3 (valid), got {submission.current_stage}"

    # 3. Admin accepts the feed (publish)
    api_client.force_authenticate(user=admin_user)
    promote_url = f'/api/data_manager/feed-submissions/{submission_id}/'
    response = api_client.patch(promote_url, {'stage': 4})
    assert response.status_code in [200, 204], response.data

    submission.refresh_from_db()
    assert submission.current_stage == 4, f"Expected stage 4, got {submission.current_stage}"

    # 4. Check if it's visible on the /feeds/ endpoint
    feeds_url = '/api/data_manager/feeds/'
    response = api_client.get(feeds_url)
    assert response.status_code == 200

    results = response.data if isinstance(response.data, list) else response.data.get('results', [])
    feed_ids = [f['id'] for f in results]
    assert submission_id in feed_ids or static_entry.id in feed_ids, "Feed not found in /feeds/ endpoint after reaching stage 4"


def test_gtfs_upload_flow_wrong(api_client, normal_user, org):
    # 1. User uploads INVALID GTFS
    api_client.force_authenticate(user=normal_user)

    gtfs_correct = os.path.join(os.path.dirname(__file__), 'GTFS_correct.zip')
    gtfs_wrong = os.path.join(os.path.dirname(__file__), 'GTFS_wrong.zip')

    upload_url = '/api/data_manager/feed-submissions/'
    payload = {
        'transport_organization': str(org.id),
        'data_type': 'gtfs',
        'name': 'Test GTFS Upload Wrong',
        'static_entry.file': open(gtfs_wrong, 'rb'),
        'static_entry.is_original': False,
    }
    response = api_client.post(upload_url, payload, format='multipart')
    assert response.status_code == 201, response.data

    submission_id = response.data['id']
    submission = FeedSubmission.objects.get(id=submission_id)

    assert submission.current_stage == 2, f"Expected stage 2, got {submission.current_stage}"

    static_entry = StaticFeedEntry.objects.get(submission=submission)

    # 2. Run validation which should fail and move back to step 1 (needs changes)
    os.environ['HOST_MEDIA_ROOT'] = str(settings.MEDIA_ROOT)

    with override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True):
        with patch('docker.from_env', _mock_docker_from_env(3)):
            validate_gtfs_feed_task.delay(static_entry.id)
        static_entry.refresh_from_db()
        assert hasattr(static_entry, 'validation_report'), f"Validation report was not created for {static_entry.id}"
        assert static_entry.validation_report.error_count > 0, "Expected errors to be found in the invalid GTFS feed"
        assert static_entry.validation_status == StaticFeedEntry.VALIDATION_INVALID

    submission.refresh_from_db()
    assert submission.current_stage == 1, f"Expected stage 1 (rejected), got {submission.current_stage}"

    rejected = FeedSubmissionHistory.objects.filter(submission=submission, event_type=FeedSubmissionHistory.EVENT_REJECTED).first()
    assert rejected is not None
    assert rejected.cause != ""


def test_jwt_token_obtain_and_refresh(api_client, normal_user):
    normal_user.first_name = 'Jan'
    normal_user.last_name = 'Kowalski'
    normal_user.save(update_fields=['first_name', 'last_name'])

    response = api_client.post(
        '/api/auth/token/',
        {'username': normal_user.username, 'password': 'password'},
        format='json',
    )
    assert response.status_code == 200, response.data
    assert response.data['access']
    assert response.data['refresh']

    access = AccessToken(response.data['access'])
    assert 'roles' in access
    assert isinstance(access['roles'], list)
    assert 'DataProvider' in access['roles']
    assert access['first_name'] == 'Jan'
    assert access['last_name'] == 'Kowalski'

    refresh_response = api_client.post(
        '/api/auth/token/refresh/',
        {'refresh': response.data['refresh']},
        format='json',
    )
    assert refresh_response.status_code == 200, refresh_response.data
    assert refresh_response.data['access']

    refreshed_access = AccessToken(refresh_response.data['access'])
    assert 'roles' in refreshed_access
    assert 'DataProvider' in refreshed_access['roles']
    assert refreshed_access['first_name'] == 'Jan'
    assert refreshed_access['last_name'] == 'Kowalski'


def test_data_provider_cannot_confirm_feed(api_client, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Needs helper confirmation',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
        actor=normal_user,
    )

    api_client.force_authenticate(user=normal_user)
    response = api_client.patch(f'/api/data_manager/feed-submissions/{submission.id}/', {'stage': 4})
    assert response.status_code == 403


def test_patch_feed_submission_keeps_url_when_json_sends_empty_url_and_null_file(
    api_client, normal_user, org,
):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='URL feed',
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/gtfs.zip',
        download_time_1=time(12, 0, 0),
        license='Old',
    )
    api_client.force_authenticate(user=normal_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'name': 'Renamed',
            'static_entry': {
                'url': '',
                'file': None,
                'license': 'MIT',
            },
        },
        format='json',
    )
    assert response.status_code == 200, response.data
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.url == 'https://example.com/gtfs.zip'
    assert entry.license == 'MIT'


def test_patch_feed_submission_keeps_file_when_json_sends_empty_url_and_null_file(
    api_client, normal_user, org, settings, tmp_path,
):
    settings.MEDIA_ROOT = tmp_path
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='File feed',
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        file=ContentFile(b'pk', name='feed.zip'),
    )
    api_client.force_authenticate(user=normal_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'static_entry': {'url': '', 'file': None},
        },
        format='json',
    )
    assert response.status_code == 200, response.data
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.file.name
    assert 'feed.zip' in entry.file.name


def test_static_entry_hide_original_without_auth_passes_validation(normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Proxy no auth',
    )
    entry = StaticFeedEntry(
        submission=submission,
        url='https://example.com/gtfs.zip',
        hide_original=True,
        download_time_1=time(10, 30, 0),
    )
    entry.full_clean()
    entry.save()


def test_owner_restricted_patch_after_stage_3(api_client, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Verified feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/gtfs.zip',
        download_time_1=time(8, 0, 0),
        download_time_2=time(20, 0, 0),
        license='Old',
        auth_type='api_key',
        auth_value='secret',
    )
    api_client.force_authenticate(user=normal_user)

    bad = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'static_entry': {
                'url': 'https://example.com/other.zip',
                'license': 'MIT',
            },
        },
        format='json',
    )
    assert bad.status_code == 400, bad.data
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.url == 'https://example.com/gtfs.zip'

    bad_name = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'name': 'Illegal rename'},
        format='json',
    )
    assert bad_name.status_code == 400, bad_name.data

    ok = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'note': 'User note',
            'static_entry': {
                'license': 'CC-BY-4.0',
                'download_time_1': '07:15:00',
                'download_time_2': None,
            },
        },
        format='json',
    )
    assert ok.status_code == 200, ok.data
    entry.refresh_from_db()
    submission.refresh_from_db()
    assert submission.name == 'Verified feed'
    assert submission.note == 'User note'
    assert entry.license == 'CC-BY-4.0'
    assert entry.download_time_1 == time(7, 15, 0)
    assert entry.download_time_2 is None
    assert entry.auth_type == 'api_key'
    assert entry.auth_value == 'secret'


def test_provider_cannot_change_is_original_after_stage_1(api_client, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Verified feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/gtfs.zip',
        download_time_1=time(8, 0, 0),
        license='Old',
        is_original=True,
        hide_original=False,
    )
    api_client.force_authenticate(user=normal_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'static_entry': {
                'is_original': False,
                'hide_original': True,
                'license': 'MIT',
            },
        },
        format='json',
    )
    assert response.status_code == 400, response.data
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.is_original is True
    assert entry.hide_original is False


def test_owner_realtime_restricted_after_stage_2(api_client, normal_user, org):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='GBFS org',
    )
    RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.com/gbfs.json',
        interval=60,
        hide_original=False,
        is_original=True,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
        actor=normal_user,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=1,
        stage_after=2,
        actor=normal_user,
    )
    api_client.force_authenticate(user=normal_user)

    bad = api_client.patch(
        f'/api/data_manager/realtime-submissions/{rts.id}/',
        {
            'endpoints': [{
                'endpoint_type': 'gbfs',
                'url': 'https://example.com/other.json',
                'interval': 60,
                'hide_original': False,
                'is_original': True,
            }],
        },
        format='json',
    )
    assert bad.status_code == 400, bad.data

    bad_name = api_client.patch(
        f'/api/data_manager/realtime-submissions/{rts.id}/',
        {'name': 'New name'},
        format='json',
    )
    assert bad_name.status_code == 400, bad_name.data

    ok = api_client.patch(
        f'/api/data_manager/realtime-submissions/{rts.id}/',
        {
            'note': 'note',
            'license': 'CC0',
            'endpoints': [{'endpoint_type': 'gbfs', 'interval': 120}],
        },
        format='json',
    )
    assert ok.status_code == 200, ok.data
    rts.refresh_from_db()
    ep = rts.endpoints.get(endpoint_type='gbfs')
    assert ep.interval == 120
    assert ep.url == 'https://example.com/gbfs.json'
    assert rts.name == 'GBFS org'
    assert rts.license == 'CC0'


def test_owner_realtime_restricted_cannot_change_hide_or_original(api_client, normal_user, org):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
    )
    RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.com/gbfs.json',
        interval=60,
        hide_original=False,
        is_original=True,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
        actor=normal_user,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=1,
        stage_after=2,
        actor=normal_user,
    )
    api_client.force_authenticate(user=normal_user)
    response = api_client.patch(
        f'/api/data_manager/realtime-submissions/{rts.id}/',
        {
            'endpoints': [{
                'endpoint_type': 'gbfs',
                'url': 'https://example.com/gbfs.json',
                'interval': 60,
                'hide_original': True,
                'is_original': False,
            }],
        },
        format='json',
    )
    assert response.status_code == 400, response.data
    ep = rts.endpoints.get(endpoint_type='gbfs')
    ep.refresh_from_db()
    assert ep.hide_original is False
    assert ep.is_original is True


def test_owner_realtime_full_edit_at_stage_1(api_client, normal_user, org):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
    )
    RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.com/a.json',
        interval=60,
        hide_original=False,
        is_original=False,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
        actor=normal_user,
    )
    api_client.force_authenticate(user=normal_user)
    response = api_client.patch(
        f'/api/data_manager/realtime-submissions/{rts.id}/',
        {
            'endpoints': [{
                'endpoint_type': 'gbfs',
                'url': 'https://example.com/b.json',
                'interval': 90,
                'hide_original': False,
                'is_original': False,
            }],
        },
        format='json',
    )
    assert response.status_code == 200, response.data
    ep = rts.endpoints.get(endpoint_type='gbfs')
    ep.refresh_from_db()
    assert ep.url == 'https://example.com/b.json'
    assert ep.interval == 90


def test_helper_patch_only_rejection_cause(api_client, helper_user, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Awaiting review',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=2,
        actor=normal_user,
    )
    api_client.force_authenticate(user=helper_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'rejection_cause': 'Missing required files.'},
        format='json',
    )
    assert response.status_code == 200, response.data
    submission.refresh_from_db()
    assert submission.is_rejected is True


def test_helper_cannot_patch_feed_content(api_client, helper_user, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='No helper edit',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=2,
        actor=normal_user,
    )
    api_client.force_authenticate(user=helper_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'note': 'Helper note'},
        format='json',
    )
    assert response.status_code == 403, response.data


def test_helper_cannot_change_url_at_stage_3(api_client, helper_user, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Helper cannot edit source',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/a.zip',
        download_time_1=time(8, 0, 0),
    )
    api_client.force_authenticate(user=helper_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'static_entry': {'url': 'https://example.com/b.zip'}},
        format='json',
    )
    assert response.status_code == 403, response.data


def test_helper_cannot_change_realtime_url_at_stage_2(api_client, helper_user, normal_user, org):
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='GBFS',
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=1,
        stage_after=2,
        actor=normal_user,
    )
    RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.com/a.json',
        interval=60,
    )
    api_client.force_authenticate(user=helper_user)
    response = api_client.patch(
        f'/api/data_manager/realtime-submissions/{rts.id}/',
        {
            'endpoints': [{
                'endpoint_type': 'gbfs',
                'url': 'https://example.com/b.json',
                'interval': 60,
            }],
        },
        format='json',
    )
    assert response.status_code == 403, response.data


@pytest.fixture
def admin_role_user():
    user = get_user_model().objects.create_user('adminrole', 'adminrole@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='Admin')
    user.groups.add(group)
    return user


def test_admin_role_can_change_url_at_stage_3(api_client, admin_role_user, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Admin role may edit source',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/a.zip',
        download_time_1=time(8, 0, 0),
    )
    api_client.force_authenticate(user=admin_role_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'static_entry': {'url': 'https://example.com/b.zip'}},
        format='json',
    )
    assert response.status_code == 200, response.data
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.url == 'https://example.com/b.zip'


def test_superuser_without_admin_role_cannot_change_url_at_stage_3(
    api_client, admin_user, normal_user, org,
):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Superuser not Admin role',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/a.zip',
        download_time_1=time(8, 0, 0),
    )
    api_client.force_authenticate(user=admin_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'static_entry': {'url': 'https://example.com/b.zip'}},
        format='json',
    )
    assert response.status_code == 400, response.data


def test_owner_cannot_change_url_at_stage_2(api_client, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Pre-verify',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=2,
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/a.zip',
        download_time_1=time(6, 0, 0),
    )
    api_client.force_authenticate(user=normal_user)
    bad = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'static_entry': {
                'url': 'https://example.com/b.zip',
                'download_time_1': '06:30:00',
            },
        },
        format='json',
    )
    assert bad.status_code == 400, bad.data
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.url == 'https://example.com/a.zip'

    ok = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'static_entry': {
                'download_time_1': '06:30:00',
            },
        },
        format='json',
    )
    assert ok.status_code == 200, ok.data
    entry.refresh_from_db()
    assert entry.download_time_1 == time(6, 30, 0)


def test_owner_cannot_change_file_at_stage_2(api_client, normal_user, org, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='File feed locked',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=2,
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        file=ContentFile(b'original', name='feed.zip'),
    )
    api_client.force_authenticate(user=normal_user)
    gtfs_path = os.path.join(os.path.dirname(__file__), 'GTFS_correct.zip')
    bad = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {
            'static_entry.file': open(gtfs_path, 'rb'),
            'static_entry.license': 'MIT',
        },
        format='multipart',
    )
    assert bad.status_code == 400, bad.data
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert 'feed.zip' in entry.file.name


def test_owner_can_change_file_when_rejected(api_client, normal_user, org, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Rejected file feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_REJECTED,
        stage_before=2,
        stage_after=1,
        cause='invalid feed',
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        file=ContentFile(b'wrong', name='wrong.zip'),
    )
    api_client.force_authenticate(user=normal_user)
    gtfs_path = os.path.join(os.path.dirname(__file__), 'GTFS_correct.zip')
    with patch('data_manager.signals.validate_gtfs_feed_task.delay') as delay_mock:
        ok = api_client.patch(
            f'/api/data_manager/feed-submissions/{submission.id}/',
            {'static_entry.file': open(gtfs_path, 'rb')},
            format='multipart',
        )
    assert ok.status_code == 200, ok.data
    delay_mock.assert_called_once()
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.file.name.endswith('.zip')
    assert 'wrong.zip' not in entry.file.name


def test_resubmit_clears_is_rejected(api_client, normal_user, org, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Resubmit after reject',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_REJECTED,
        stage_before=2,
        stage_after=1,
        cause='fix required',
        actor=normal_user,
    )
    StaticFeedEntry.objects.create(
        submission=submission,
        url='https://example.com/old.zip',
        download_time_1=time(8, 0, 0),
    )
    api_client.force_authenticate(user=normal_user)
    with patch('data_manager.signals.validate_gtfs_feed_task.delay'):
        response = api_client.patch(
            f'/api/data_manager/feed-submissions/{submission.id}/',
            {
                'static_entry': {
                    'url': 'https://example.com/new.zip',
                    'download_time_1': '09:00:00',
                },
            },
            format='json',
        )
    assert response.status_code == 200, response.data
    submission.refresh_from_db()
    assert submission.is_rejected is False
    assert submission.rejection_cause is None
    assert submission.current_stage == 2
    entry = StaticFeedEntry.objects.get(submission=submission)
    assert entry.url == 'https://example.com/new.zip'


def test_validate_gtfs_task_uses_feeds_queue():
    assert settings.CELERY_TASK_ROUTES['data_manager.validate_gtfs_feed']['queue'] == 'feeds'


def test_patch_feed_submission_static_entry_without_source_returns_400(
    api_client, normal_user, org,
):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='No entry yet',
    )
    api_client.force_authenticate(user=normal_user)
    response = api_client.patch(
        f'/api/data_manager/feed-submissions/{submission.id}/',
        {'static_entry': {'license': 'MIT'}},
        format='json',
    )
    assert response.status_code == 400
    assert 'static_entry' in response.data


def test_helper_can_confirm_feed_but_cannot_create_feed(api_client, helper_user, normal_user, org):
    submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Ready for helper confirmation',
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
        actor=normal_user,
    )

    api_client.force_authenticate(user=helper_user)
    create_response = api_client.post(
        '/api/data_manager/feed-submissions/',
        {'transport_organization': str(org.id), 'data_type': 'gtfs', 'name': 'Helper upload'},
    )
    assert create_response.status_code == 403

    confirm_response = api_client.patch(f'/api/data_manager/feed-submissions/{submission.id}/', {'stage': 4})
    assert confirm_response.status_code == 200, confirm_response.data
    submission.refresh_from_db()
    assert submission.current_stage == 4


def test_user_submissions_endpoint_combines_static_and_realtime(api_client, normal_user, org):
    other_user = get_user_model().objects.create_user('other', 'other@example.com', 'password')
    group, _ = Group.objects.get_or_create(name='DataProvider')
    other_user.groups.add(group)

    static_submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Own static feed',
    )
    realtime_submission = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='Own realtime feed',
    )
    FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=other_user,
        data_type='gtfs',
        name='Other user static feed',
    )
    RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=other_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='Other user realtime feed',
    )

    api_client.force_authenticate(user=normal_user)
    response = api_client.get('/api/data_manager/submissions/')

    assert response.status_code == 200, response.data
    assert response.data['user'] == normal_user.id
    assert [item['id'] for item in response.data['static']] == [static_submission.id]
    assert response.data['static'][0]['transport_organization'] == org.transport_organization
    assert [item['id'] for item in response.data['realtime']] == [realtime_submission.id]
    assert response.data['realtime'][0]['transport_organization'] == org.transport_organization
    assert response.data['realtime'][0]['protocol'] == RealtimeSubmission.PROTOCOL_GBFS
    assert 'endpoints' not in response.data['realtime'][0]
    assert 'history' not in response.data['realtime'][0]
    assert 'note' not in response.data['realtime'][0]
    assert 'license' not in response.data['realtime'][0]


def test_realtime_submission_static_feed_must_match_organization(normal_user, org):
    other_org = TransportOrganization.objects.create(region="Other Region", transport_organization="Other Org")
    static_submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Published static feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=static_submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=normal_user,
    )

    realtime = RealtimeSubmission(
        transport_organization=other_org,
        submitted_by=normal_user,
        static_submission=static_submission,
        protocol=RealtimeSubmission.PROTOCOL_GTFS_RT,
        name='Mismatched realtime feed',
    )

    with pytest.raises(ValidationError) as exc:
        realtime.full_clean()

    assert 'static_submission' in exc.value.message_dict


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
def test_realtime_validation_advances_to_stage_3_and_caches_proxy(org, normal_user, settings, tmp_path):
    from unittest.mock import MagicMock

    from data_manager.tasks import validate_realtime_submission_task

    settings.MEDIA_ROOT = tmp_path
    rts = RealtimeSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='GBFS verify',
    )
    RealtimeEndpointRT.objects.create(
        submission=rts,
        endpoint_type='gbfs',
        url='https://example.com/gbfs.json',
        interval=60,
        hide_original=True,
        is_original=True,
    )
    RealtimeSubmissionHistory.objects.create(
        submission=rts,
        event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
        stage_before=0,
        stage_after=1,
        actor=normal_user,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"data":{}}'
    mock_response.raise_for_status = MagicMock()

    with (
        patch('data_manager.net_security.safe_get', return_value=mock_response),
        patch('data_manager.scheduler.safe_get', return_value=mock_response),
    ):
        result = validate_realtime_submission_task(rts.id)

    assert result['status'] == 'ok'
    rts.refresh_from_db()
    assert rts.current_stage == 3
    ep = rts.endpoints.get(endpoint_type='gbfs')
    assert ep.cached_file


def test_eligible_static_submissions_for_realtime_endpoint(api_client, normal_user, org):
    gtfs_submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Published GTFS feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=gtfs_submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=normal_user,
    )
    netex_submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='netex',
        name='Published NeTEx feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=netex_submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=normal_user,
    )
    other_submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='other',
        name='Published unsupported feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=other_submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=normal_user,
    )

    api_client.force_authenticate(user=normal_user)
    response = api_client.get(
        f'/api/data_manager/realtime-submissions/eligible-static-submissions/{org.id}/',
    )

    assert response.status_code == 200, response.data
    by_id = {item['id']: item for item in response.data}
    assert by_id[gtfs_submission.id]['allowed_realtime_protocols'] == ['gtfs_rt']
    assert by_id[netex_submission.id]['allowed_realtime_protocols'] == ['siri']
    assert other_submission.id not in by_id

    gtfs_response = api_client.get(
        f'/api/data_manager/realtime-submissions/eligible-static-submissions/{org.id}/gtfs/',
    )
    assert gtfs_response.status_code == 200, gtfs_response.data
    assert [item['id'] for item in gtfs_response.data] == [gtfs_submission.id]

    netex_response = api_client.get(
        f'/api/data_manager/realtime-submissions/eligible-static-submissions/{org.id}/netex/',
    )
    assert netex_response.status_code == 200, netex_response.data
    assert [item['id'] for item in netex_response.data] == [netex_submission.id]

    other_response = api_client.get(
        f'/api/data_manager/realtime-submissions/eligible-static-submissions/{org.id}/other/',
    )
    assert other_response.status_code == 200, other_response.data
    assert other_response.data == []


def test_gbfs_cannot_be_linked_to_static_submission(normal_user, org):
    static_submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='gtfs',
        name='Published GTFS feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=static_submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=normal_user,
    )

    realtime = RealtimeSubmission(
        transport_organization=org,
        submitted_by=normal_user,
        static_submission=static_submission,
        protocol=RealtimeSubmission.PROTOCOL_GBFS,
        name='Invalid GBFS attachment',
    )

    with pytest.raises(ValidationError) as exc:
        realtime.full_clean()

    assert 'static_submission' in exc.value.message_dict


def test_realtime_protocol_must_match_static_feed_type(normal_user, org):
    netex_submission = FeedSubmission.objects.create(
        transport_organization=org,
        submitted_by=normal_user,
        data_type='netex',
        name='Published NeTEx feed',
    )
    FeedSubmissionHistory.objects.create(
        submission=netex_submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
        actor=normal_user,
    )

    realtime = RealtimeSubmission(
        transport_organization=org,
        submitted_by=normal_user,
        static_submission=netex_submission,
        protocol=RealtimeSubmission.PROTOCOL_GTFS_RT,
        name='Invalid GTFS-RT attachment',
    )

    with pytest.raises(ValidationError) as exc:
        realtime.full_clean()

    assert 'static_submission' in exc.value.message_dict
