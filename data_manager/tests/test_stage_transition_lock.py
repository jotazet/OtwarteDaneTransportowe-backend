"""Stage transitions of the API and the validation tasks share a row lock (issue #52).

A reviewer's PATCH, an owner's resubmission and a validation task each lock the
submission and read its stage from committed history before writing. A request
whose stage changed after ``get_object()`` read it answers 409
``feeds.stage_changed`` and saves nothing. The change is simulated by writing a
history row right after ``get_object()``, the window a concurrent validation run
or reviewer used to hit.
"""

import os
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from data_manager.api.views import FeedSubmissionViewSet, RealtimeSubmissionViewSet
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)
from data_manager.tasks import validate_gtfs_feed_task
from data_manager.tests import test_role_matrix as role_matrix
from data_manager.tests.test_flow import _mock_docker_from_env

# The role-matrix fixtures: pytest finds fixtures by module attribute.
client_for = role_matrix.client_for
org = role_matrix.org
owner = role_matrix.owner
realtime_submission = role_matrix.realtime_submission
static_submission = role_matrix.static_submission
submissions = role_matrix.submissions

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('fake_dns')]

API = '/api/data_manager'
VIEWSETS = {
    'feed-submissions': (FeedSubmissionViewSet, FeedSubmissionHistory),
    'realtime-submissions': (RealtimeSubmissionViewSet, RealtimeSubmissionHistory),
}


def _write_after_get_object(monkeypatch, viewset, **history):
    """Write a history row right after ``get_object()`` read the submission."""
    get_object = viewset.get_object

    def get_object_then_write(self):
        submission = get_object(self)
        submission.history.model.objects.create(submission=submission, **history)
        return submission

    monkeypatch.setattr(viewset, 'get_object', get_object_then_write)


@pytest.mark.parametrize('kind', VIEWSETS)
@pytest.mark.parametrize('actor', ['helper', 'admin'])
def test_publish_after_a_concurrent_rejection_is_409(
    kind, actor, submissions, client_for, monkeypatch
):
    submission = submissions[kind]
    viewset, history_model = VIEWSETS[kind]
    _write_after_get_object(
        monkeypatch,
        viewset,
        event_type=history_model.EVENT_REJECTED,
        stage_before=submission.current_stage,
        stage_after=1,
        cause='validation failed',
    )

    response = client_for(actor).patch(
        f'{API}/{kind}/{submission.id}/', {'stage': 4}, format='json'
    )

    assert response.status_code == 409
    assert response.data == {'code': 'feeds.stage_changed', 'details': {}}
    submission = type(submission).objects.get(pk=submission.pk)
    assert submission.is_rejected
    assert not submission.history.filter(event_type=history_model.EVENT_COMPLETED).exists()


@pytest.mark.parametrize('kind', VIEWSETS)
def test_rejection_after_a_concurrent_publish_is_409(kind, submissions, client_for, monkeypatch):
    """Two reviewers at once: the second one's stage_before would be stale."""
    submission = submissions[kind]
    viewset, history_model = VIEWSETS[kind]
    _write_after_get_object(
        monkeypatch,
        viewset,
        event_type=history_model.EVENT_COMPLETED,
        stage_before=submission.current_stage,
        stage_after=4,
    )

    response = client_for('helper').patch(
        f'{API}/{kind}/{submission.id}/', {'rejection_cause': 'broken'}, format='json'
    )

    assert response.status_code == 409
    submission = type(submission).objects.get(pk=submission.pk)
    assert submission.current_stage == 4
    assert not submission.is_rejected


def test_admin_content_edit_is_rolled_back_on_409(static_submission, client_for, monkeypatch):
    _write_after_get_object(
        monkeypatch,
        FeedSubmissionViewSet,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=2,
        stage_after=3,
    )

    response = client_for('admin').patch(
        f'{API}/feed-submissions/{static_submission.id}/',
        {'name': 'Renamed', 'stage': 4},
        format='json',
    )

    assert response.status_code == 409
    static_submission.refresh_from_db()
    assert static_submission.name == 'Static'


def _reject(submission, history_model):
    history_model.objects.create(
        submission=submission,
        event_type=history_model.EVENT_REJECTED,
        stage_before=submission.current_stage,
        stage_after=1,
        cause='fix it',
    )


def test_static_resubmission_after_a_concurrent_review_is_409(
    static_submission, client_for, monkeypatch
):
    """The owner may change the source only while rejected; a reviewer moved it on."""
    _reject(static_submission, FeedSubmissionHistory)
    _write_after_get_object(
        monkeypatch,
        FeedSubmissionViewSet,
        event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=1,
        stage_after=3,
    )
    history_before = static_submission.history.count() + 1  # plus the row written above

    with patch(
        'data_manager.api.serializers.FeedSubmissionWriteSerializer._schedule_source_refresh'
    ):
        response = client_for('owner').patch(
            f'{API}/feed-submissions/{static_submission.id}/',
            {'static_entry': {'url': 'https://example.org/other.zip'}},
            format='json',
        )

    assert response.status_code == 409
    entry = StaticFeedEntry.objects.get(submission=static_submission)
    assert entry.url == 'https://example.org/gtfs.zip'
    submission = FeedSubmission.objects.get(pk=static_submission.pk)
    assert submission.current_stage == 3
    assert submission.history.count() == history_before


def test_realtime_resubmission_after_a_concurrent_review_is_409(
    realtime_submission, client_for, monkeypatch
):
    _reject(realtime_submission, RealtimeSubmissionHistory)
    _write_after_get_object(
        monkeypatch,
        RealtimeSubmissionViewSet,
        event_type=RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED,
        stage_before=1,
        stage_after=3,
    )
    history_before = realtime_submission.history.count() + 1  # plus the row written above

    with patch('data_manager.tasks.enqueue') as enqueue:
        response = client_for('owner').patch(
            f'{API}/realtime-submissions/{realtime_submission.id}/',
            {
                'endpoints': [
                    {
                        'endpoint_type': 'gbfs',
                        'url': 'https://example.org/other.json',
                        'interval': 60,
                    }
                ]
            },
            format='json',
        )

    assert response.status_code == 409
    enqueue.assert_not_called()
    rts = RealtimeSubmission.objects.get(pk=realtime_submission.pk)
    assert rts.current_stage == 3
    assert rts.history.count() == history_before
    assert rts.endpoints.get().url == 'https://example.org/gbfs.json'


@pytest.mark.parametrize('kind', VIEWSETS)
@pytest.mark.parametrize('actor', ['helper', 'admin'])
def test_reviewer_transition_locks_the_submission(kind, actor, submissions, client_for):
    submission = submissions[kind]
    stage_before = submission.current_stage
    table = type(submission)._meta.db_table

    with CaptureQueriesContext(connection) as queries:
        response = client_for(actor).patch(
            f'{API}/{kind}/{submission.id}/', {'stage': 3}, format='json'
        )

    assert response.status_code == 200
    assert any(
        'FOR UPDATE' in q['sql'] and f'FROM "{table}"' in q['sql'] for q in queries.captured_queries
    )
    history = type(submission).objects.get(pk=submission.pk).history.first()
    assert (history.stage_before, history.stage_after) == (stage_before, 3)


def test_failed_validation_after_publish_keeps_the_feed_published(
    static_submission, client_for, settings
):
    """A validation run that finishes after a reviewer published never unpublishes."""
    entry = StaticFeedEntry.objects.get(submission=static_submission)
    with open(os.path.join(os.path.dirname(__file__), 'GTFS_wrong.zip'), 'rb') as fh:
        entry.file.save('gtfs.zip', fh)
    entry.url = ''
    entry.hide_original = False
    entry.save()

    response = client_for('admin').patch(
        f'{API}/feed-submissions/{static_submission.id}/', {'stage': 4}, format='json'
    )
    assert response.status_code == 200

    settings.HOST_MEDIA_ROOT = str(settings.MEDIA_ROOT)
    with patch('docker.from_env', _mock_docker_from_env(3)):
        validate_gtfs_feed_task(entry.id)

    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_INVALID
    submission = FeedSubmission.objects.get(pk=static_submission.pk)
    assert submission.current_stage == 4
    assert not submission.history.filter(event_type=FeedSubmissionHistory.EVENT_REJECTED).exists()
