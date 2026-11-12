"""The admin action ``advance_to_next_stage`` follows the API's stage rules.

It needs Django's ``change`` permission on FeedSubmission and a reviewer role
(Helper/Admin group, or superuser); ``is_staff`` or model permissions alone are
not enough.
"""

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, FeedSubmissionHistory
from OtwarteDaneTransportowe.auth_roles import (
    ROLE_ADMIN,
    ROLE_DATA_PROVIDER,
    ROLE_HELPER,
)

pytestmark = pytest.mark.django_db

CHANGELIST = 'admin:data_manager_feedsubmission_changelist'
ACTION = 'advance_to_next_stage'


def _staff(make_user, role, *codenames):
    user = make_user(role) if role else make_user(ROLE_DATA_PROVIDER)
    if role is None:
        user.groups.clear()
    user.is_staff = True
    user.save()
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label='data_manager',
            content_type__model='feedsubmission',
            codename__in=codenames,
        )
    )
    return user


@pytest.fixture
def org():
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


@pytest.fixture
def make_submission(org, make_user):
    owner = make_user(ROLE_DATA_PROVIDER)

    def make(stage=2, rejected=False):
        submission = FeedSubmission.objects.create(
            transport_organization=org, submitted_by=owner, data_type='gtfs', name='Feed'
        )
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=(
                FeedSubmissionHistory.EVENT_REJECTED
                if rejected
                else FeedSubmissionHistory.EVENT_UPLOADED
            ),
            stage_before=stage,
            stage_after=1 if rejected else stage,
            actor=owner,
        )
        return submission

    return make


def _run_action(user, *submissions):
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client.post(
        reverse(CHANGELIST),
        {'action': ACTION, '_selected_action': [s.pk for s in submissions]},
        follow=True,
    )


def _stage(submission):
    return FeedSubmission.objects.get(pk=submission.pk).current_stage


def _offered_actions(user):
    from django.test import Client

    client = Client()
    client.force_login(user)
    response = client.get(reverse(CHANGELIST))
    assert response.status_code == 200
    form = response.context['action_form']  # None when no action is offered at all
    return set() if form is None else {value for value, _ in form.fields['action'].choices}


def test_action_is_offered_only_to_reviewers_with_change_permission(make_user, make_submission):
    make_submission()
    view_only = _staff(make_user, None, 'view_feedsubmission')
    helper = _staff(make_user, ROLE_HELPER, 'view_feedsubmission', 'change_feedsubmission')
    assert ACTION not in _offered_actions(view_only)
    assert ACTION in _offered_actions(helper)


def test_view_only_staff_cannot_run_the_action(make_user, make_submission):
    submission = make_submission(stage=2)
    _run_action(_staff(make_user, None, 'view_feedsubmission'), submission)
    assert _stage(submission) == 2


def test_staff_with_change_permission_but_no_role_cannot_run_the_action(make_user, make_submission):
    submission = make_submission(stage=2)
    _run_action(_staff(make_user, None, 'view_feedsubmission', 'change_feedsubmission'), submission)
    assert _stage(submission) == 2


def test_helper_without_django_change_permission_cannot_run_the_action(make_user, make_submission):
    submission = make_submission(stage=2)
    _run_action(_staff(make_user, ROLE_HELPER, 'view_feedsubmission'), submission)
    assert _stage(submission) == 2


@pytest.mark.parametrize('role', [ROLE_HELPER, ROLE_ADMIN])
def test_reviewer_with_change_permission_advances_submission(role, make_user, make_submission):
    submission = make_submission(stage=2)
    user = _staff(make_user, role, 'view_feedsubmission', 'change_feedsubmission')
    _run_action(user, submission)
    assert _stage(submission) == 3
    _run_action(user, submission)
    assert _stage(submission) == 4
    assert submission.history.filter(event_type=FeedSubmissionHistory.EVENT_COMPLETED).count() == 1


def test_superuser_can_run_the_action(django_user_model, make_submission):
    submission = make_submission(stage=2)
    user = django_user_model.objects.create_superuser('root', 'root@example.org', 'x')
    _run_action(user, submission)
    assert _stage(submission) == 3


def test_rejected_and_published_submissions_are_skipped(make_user, make_submission):
    rejected = make_submission(stage=2, rejected=True)
    published = make_submission(stage=4)
    pending = make_submission(stage=2)
    user = _staff(make_user, ROLE_HELPER, 'view_feedsubmission', 'change_feedsubmission')
    _run_action(user, rejected, published, pending)
    assert _stage(rejected) == 1
    assert FeedSubmission.objects.get(pk=rejected.pk).is_rejected
    assert _stage(published) == 4
    assert published.history.count() == 1
    assert _stage(pending) == 3
