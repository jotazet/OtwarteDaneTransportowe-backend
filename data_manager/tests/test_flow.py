import pytest
import os
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, StaticFeedEntry, FeedSubmissionHistory
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
    return user.objects.create_user('user', 'user@example.com', 'password')

@pytest.fixture
def org(normal_user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region="Test Region", transport_organization="Test Org")


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
        validate_gtfs_feed_task.delay(static_entry.id)
        static_entry.refresh_from_db()
        assert hasattr(static_entry, 'validation_report'), f"Validation report was not created for {static_entry.id}"
        assert static_entry.validation_report.error_count == 0, f"Expected no errors in the valid GTFS feed, but got {static_entry.validation_report.error_count}"

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
        validate_gtfs_feed_task.delay(static_entry.id)
        static_entry.refresh_from_db()
        assert hasattr(static_entry, 'validation_report'), f"Validation report was not created for {static_entry.id}"
        assert static_entry.validation_report.error_count > 0, "Expected errors to be found in the invalid GTFS feed"

    submission.refresh_from_db()
    assert submission.current_stage == 1, f"Expected stage 1 (rejected), got {submission.current_stage}"

    rejected = FeedSubmissionHistory.objects.filter(submission=submission, event_type=FeedSubmissionHistory.EVENT_REJECTED).first()
    assert rejected is not None
    assert rejected.cause != ""
