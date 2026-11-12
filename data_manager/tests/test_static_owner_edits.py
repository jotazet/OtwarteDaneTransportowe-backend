"""Owner edits of static submissions: stage-1 resubmission (#53) and full PUT (#54)."""

from datetime import time
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from rest_framework.test import APIClient

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, FeedSubmissionHistory, StaticFeedEntry

pytestmark = pytest.mark.django_db


def _user(username, group):
    user = get_user_model().objects.create_user(username, f'{username}@example.com', 'password')
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def owner():
    return _user('owner', 'DataProvider')


@pytest.fixture
def helper():
    return _user('helper', 'Helper')


@pytest.fixture
def admin():
    return _user('admin-role', 'Admin')


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return TransportOrganization.objects.create(region='Test Region', transport_organization='Org')


def _submission(org, owner, data_type, stage, **entry):
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type=data_type, name='Feed'
    )
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=2,
        actor=owner,
    )
    if stage == 3:
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
            stage_before=2,
            stage_after=3,
            actor=None,
        )
    entry = entry or {'url': 'https://example.com/old.zip', 'download_time_1': time(3, 0)}
    StaticFeedEntry.objects.create(submission=submission, **entry)
    return submission


def _url(submission):
    return f'/api/data_manager/feed-submissions/{submission.id}/'


def _demote_without_cause(api_client, reviewer, submission):
    api_client.force_authenticate(user=reviewer)
    response = api_client.patch(_url(submission), {'stage': 1}, format='json')
    assert response.status_code == 200, response.data
    submission = FeedSubmission.objects.get(pk=submission.pk)
    assert submission.current_stage == 1
    assert not submission.is_rejected
    return submission


def _uploaded_rows(submission):
    return FeedSubmissionHistory.objects.filter(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_UPLOADED,
        stage_before=1,
        stage_after=2,
    ).count()


# --- #53: a submission demoted to stage 1 without a cause --------------------


@pytest.mark.parametrize('reviewer', ['helper', 'admin'])
@pytest.mark.parametrize('data_type', ['gtfs', 'netex', 'other'])
def test_owner_url_change_after_demotion_restarts_verification(
    api_client, owner, org, request, reviewer, data_type, django_capture_on_commit_callbacks
):
    submission = _submission(org, owner, data_type, stage=3)
    _demote_without_cause(api_client, request.getfixturevalue(reviewer), submission)

    api_client.force_authenticate(user=owner)
    with patch('data_manager.tasks.validate_gtfs_feed_task.delay') as validate_mock:
        with django_capture_on_commit_callbacks(execute=True):
            response = api_client.patch(
                _url(submission),
                {'static_entry': {'url': 'https://example.com/new.zip'}},
                format='json',
            )
    assert response.status_code == 200, response.data
    assert response.data['current_stage'] == 2
    assert _uploaded_rows(submission) == 2
    if data_type == 'gtfs':
        validate_mock.assert_called_once_with(submission.static_entry.id)
    else:
        validate_mock.assert_not_called()


def test_owner_file_upload_after_demotion_restarts_verification(api_client, owner, admin, org):
    submission = _submission(
        org, owner, 'netex', stage=3, file=ContentFile(b'<old/>', name='old.xml')
    )
    _demote_without_cause(api_client, admin, submission)

    api_client.force_authenticate(user=owner)
    response = api_client.patch(
        _url(submission),
        {'static_entry.file': ContentFile(b'<new/>', name='new.xml')},
        format='multipart',
    )
    assert response.status_code == 200, response.data
    assert response.data['current_stage'] == 2


def test_owner_edit_without_source_change_stays_at_stage_1(api_client, owner, helper, org):
    submission = _submission(org, owner, 'netex', stage=3)
    _demote_without_cause(api_client, helper, submission)

    api_client.force_authenticate(user=owner)
    response = api_client.patch(_url(submission), {'note': 'Fixing it'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['current_stage'] == 1


def test_admin_source_edit_after_demotion_keeps_stage(api_client, owner, admin, org):
    submission = _submission(org, owner, 'netex', stage=3)
    _demote_without_cause(api_client, admin, submission)

    response = api_client.patch(
        _url(submission),
        {'static_entry': {'url': 'https://example.com/new.zip'}},
        format='json',
    )
    assert response.status_code == 200, response.data
    assert response.data['current_stage'] == 1


# --- #54: full PUT after stage 1 ----------------------------------------------


def _put_body(submission, **overrides):
    return {
        'transport_organization': submission.transport_organization_id,
        'data_type': submission.data_type,
        'name': submission.name,
        'note': 'Updated note',
        'static_entry': {'license': 'CC-BY-4.0'},
        **overrides,
    }


def test_owner_put_with_unchanged_values_succeeds(api_client, owner, org):
    submission = _submission(org, owner, 'gtfs', stage=3)
    api_client.force_authenticate(user=owner)

    response = api_client.put(_url(submission), _put_body(submission), format='json')

    assert response.status_code == 200, response.data
    submission.refresh_from_db()
    assert submission.note == 'Updated note'
    assert submission.static_entry.license == 'CC-BY-4.0'
    assert submission.current_stage == 3


@pytest.mark.parametrize('field', ['transport_organization', 'data_type'])
def test_owner_put_with_changed_value_fails(api_client, owner, org, field):
    submission = _submission(org, owner, 'gtfs', stage=3)
    other_org = TransportOrganization.objects.create(region='R', transport_organization='Other')
    changed = {'transport_organization': other_org.id, 'data_type': 'netex'}[field]
    api_client.force_authenticate(user=owner)

    response = api_client.put(
        _url(submission), _put_body(submission, **{field: changed}), format='json'
    )

    assert response.status_code == 400, response.data
    assert field in response.data
    submission.refresh_from_db()
    assert submission.transport_organization_id == org.id
    assert submission.data_type == 'gtfs'


def test_helper_put_is_forbidden(api_client, owner, helper, org):
    submission = _submission(org, owner, 'gtfs', stage=3)
    api_client.force_authenticate(user=helper)

    response = api_client.put(_url(submission), _put_body(submission), format='json')

    assert response.status_code == 403, response.data


def test_admin_put_may_change_data_type(api_client, owner, admin, org):
    submission = _submission(org, owner, 'gtfs', stage=3)
    api_client.force_authenticate(user=admin)

    response = api_client.put(
        _url(submission), _put_body(submission, data_type='netex'), format='json'
    )

    assert response.status_code == 200, response.data
    submission.refresh_from_db()
    assert submission.data_type == 'netex'
