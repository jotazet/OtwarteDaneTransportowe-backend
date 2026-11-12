"""GTFS validation that cannot run is not the provider's fault (issue #41).

Infrastructure failures (Docker unreachable, validator container failure,
missing or broken report.json, time limit, file missing on disk) used to reject
the submission back to stage 1 with raw exception text as the cause. Now they
keep the stage, set ``validation_status=error`` with a code from
``data_manager.validation_errors`` and are picked up by
``manage.py retry_gtfs_validation``. Errors in the feed itself still reject.
"""

import json
import os
from unittest.mock import patch

import docker
import pytest
import requests
from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command

from cases.models import TransportOrganization
from data_manager import validation_errors as ve
from data_manager.models import FeedSubmission, FeedSubmissionHistory, StaticFeedEntry
from data_manager.tasks import validate_gtfs_feed_task

pytestmark = pytest.mark.django_db

# History rows that put a new submission at each stage.
_STAGE_PATHS = {
    1: [(FeedSubmissionHistory.EVENT_UPLOADED, 1, 2), (FeedSubmissionHistory.EVENT_REJECTED, 2, 1)],
    2: [(FeedSubmissionHistory.EVENT_UPLOADED, 1, 2)],
    3: [
        (FeedSubmissionHistory.EVENT_UPLOADED, 1, 2),
        (FeedSubmissionHistory.EVENT_STAGE_ADVANCED, 2, 3),
    ],
    4: [
        (FeedSubmissionHistory.EVENT_UPLOADED, 1, 2),
        (FeedSubmissionHistory.EVENT_STAGE_ADVANCED, 2, 3),
        (FeedSubmissionHistory.EVENT_COMPLETED, 3, 4),
    ],
}


@pytest.fixture
def user():
    return get_user_model().objects.create_user('provider', 'p@example.com', 'password')


@pytest.fixture
def org(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.HOST_MEDIA_ROOT = str(tmp_path)
    return TransportOrganization.objects.create(region='R', transport_organization='Org')


def _entry(org, user, stage=2, **kwargs):
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=user, data_type='gtfs', name='F'
    )
    for event_type, before, after in _STAGE_PATHS[stage]:
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=event_type,
            stage_before=before,
            stage_after=after,
            cause='earlier reviewer cause'
            if event_type == FeedSubmissionHistory.EVENT_REJECTED
            else '',
        )
    kwargs.setdefault('file', ContentFile(b'PK\x03\x04data', name='feed.zip'))
    return StaticFeedEntry.objects.create(submission=submission, **kwargs)


def _docker(run):
    """docker.from_env() stand-in whose containers.run() calls ``run(volumes)``."""

    class _Containers:
        def run(self, *, volumes, **kwargs):
            return run(volumes)

    class _Client:
        containers = _Containers()

    return lambda: _Client()


def _write_report(content: str):
    def run(volumes):
        output = next(host for host, spec in volumes.items() if spec['bind'] == '/output')
        with open(os.path.join(output, 'report.json'), 'w', encoding='utf-8') as fh:
            fh.write(content)
        return b''

    return run


def _raise(exc):
    def run(*args, **kwargs):
        raise exc

    return run


def _report(error_count: int) -> str:
    notices = (
        [{'severity': 'ERROR', 'totalNotices': error_count, 'code': 'missing_required_file'}]
        if error_count
        else []
    )
    return json.dumps({'summary': {}, 'notices': notices})


def _rejections(entry):
    return entry.submission.history.filter(event_type=FeedSubmissionHistory.EVENT_REJECTED)


_CONTAINER_OOM = docker.errors.ContainerError(
    'c', 137, ['-i', '/input/feed.zip'], 'gtfs-validator', b'Killed'
)


@pytest.mark.parametrize('stage', [2, 3])
@pytest.mark.parametrize(
    ('from_env', 'code'),
    [
        pytest.param(
            _raise(docker.errors.DockerException('Connection refused')),
            ve.VALIDATOR_UNAVAILABLE,
            id='docker-proxy-down',
        ),
        pytest.param(
            _docker(_raise(_CONTAINER_OOM)),
            ve.VALIDATOR_FAILED,
            id='container-oom',
        ),
        pytest.param(
            _docker(_raise(requests.exceptions.ConnectionError('proxy went away'))),
            ve.VALIDATOR_UNAVAILABLE,
            id='docker-api-error',
        ),
        pytest.param(
            _docker(_raise(SoftTimeLimitExceeded())),
            ve.TIMEOUT,
            id='soft-time-limit',
        ),
        pytest.param(_docker(lambda volumes: b''), ve.VALIDATOR_FAILED, id='no-report'),
        pytest.param(_docker(_write_report('{not json')), ve.VALIDATOR_FAILED, id='broken-report'),
    ],
)
def test_infrastructure_failure_keeps_stage_and_sets_error_code(org, user, stage, from_env, code):
    entry = _entry(org, user, stage=stage)
    rejections_before = _rejections(entry).count()

    with patch('docker.from_env', from_env), patch('data_manager.tasks.time.sleep'):
        validate_gtfs_feed_task(entry.id)

    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_ERROR
    assert entry.validation_message == code
    assert entry.submission.current_stage == stage
    assert _rejections(entry).count() == rejections_before


def test_feed_file_missing_on_disk_is_an_infrastructure_error(org, user):
    entry = _entry(org, user)
    os.remove(entry.file.path)

    with patch('docker.from_env') as from_env:
        validate_gtfs_feed_task(entry.id)

    from_env.assert_not_called()
    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_ERROR
    assert entry.validation_message == ve.FILE_MISSING
    assert entry.submission.current_stage == 2
    assert not _rejections(entry).exists()


def test_entry_without_any_source_is_an_infrastructure_error(org, user):
    entry = _entry(org, user, file=None)

    validate_gtfs_feed_task(entry.id)

    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_ERROR
    assert entry.validation_message == ve.FILE_MISSING
    assert entry.submission.current_stage == 2


@pytest.mark.parametrize('stage', [2, 3])
def test_errors_in_the_feed_still_reject_to_stage_1(org, user, stage):
    entry = _entry(org, user, stage=stage)

    with patch('docker.from_env', _docker(_write_report(_report(4)))):
        validate_gtfs_feed_task(entry.id)

    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_INVALID
    assert entry.validation_message == 'missing_required_file: 4'
    assert entry.submission.current_stage == 1
    rejection = _rejections(entry).get()
    assert (rejection.stage_before, rejection.cause) == (stage, 'missing_required_file: 4')


def test_errors_in_a_published_feed_do_not_unpublish_it(org, user):
    entry = _entry(org, user, stage=4)

    with patch('docker.from_env', _docker(_write_report(_report(4)))):
        validate_gtfs_feed_task(entry.id)

    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_INVALID
    assert entry.submission.current_stage == 4
    assert not _rejections(entry).exists()


def test_valid_feed_after_an_infrastructure_error_advances_to_stage_3(org, user):
    entry = _entry(org, user)
    with patch('docker.from_env', _raise(docker.errors.DockerException('down'))):
        validate_gtfs_feed_task(entry.id)

    with patch('docker.from_env', _docker(_write_report(_report(0)))):
        validate_gtfs_feed_task(entry.id)

    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_VALID
    assert entry.validation_message == ''
    assert entry.submission.current_stage == 3


def test_failed_download_of_the_providers_url_still_rejects(org, user):
    entry = _entry(org, user, file=None, url='https://example.org/feed.zip', hide_original=False)

    def failing_download(*args, **kwargs):
        raise requests.exceptions.ConnectionError('refused')

    with patch('data_manager.net_security.safe_download', failing_download):
        validate_gtfs_feed_task(entry.id)

    entry.refresh_from_db()
    assert entry.validation_status == StaticFeedEntry.VALIDATION_INVALID
    assert entry.submission.current_stage == 1
    assert _rejections(entry).count() == 1


# ---------------------------------------------------------------------------
# manage.py retry_gtfs_validation
# ---------------------------------------------------------------------------


def _retried(**options) -> list[int]:
    with patch(
        'data_manager.management.commands.retry_gtfs_validation.validate_gtfs_feed_task.delay'
    ) as delay:
        call_command('retry_gtfs_validation', **options)
    return sorted(call.args[0] for call in delay.call_args_list)


@pytest.mark.parametrize(
    ('stage', 'status', 'retried'),
    [
        (2, StaticFeedEntry.VALIDATION_ERROR, True),
        (2, StaticFeedEntry.VALIDATION_PENDING, True),
        (3, StaticFeedEntry.VALIDATION_ERROR, True),
        (2, StaticFeedEntry.VALIDATION_VALID, False),
        (3, StaticFeedEntry.VALIDATION_VALID, False),
        (1, StaticFeedEntry.VALIDATION_INVALID, False),
        (1, StaticFeedEntry.VALIDATION_ERROR, False),
        (4, StaticFeedEntry.VALIDATION_ERROR, False),
    ],
)
def test_retry_command_requeues_unfinished_validations(org, user, stage, status, retried):
    entry = _entry(org, user, stage=stage)
    StaticFeedEntry.objects.filter(pk=entry.pk).update(validation_status=status)

    assert _retried() == ([entry.id] if retried else [])


def test_retry_command_requeues_named_submissions_whatever_their_status(org, user):
    entry = _entry(org, user, stage=3)
    StaticFeedEntry.objects.filter(pk=entry.pk).update(
        validation_status=StaticFeedEntry.VALIDATION_VALID
    )

    assert _retried(submission_ids=[entry.submission_id]) == [entry.id]


def test_retry_command_recovers_an_infrastructure_error(org, user):
    entry = _entry(org, user)
    with patch('docker.from_env', _raise(docker.errors.DockerException('down'))):
        validate_gtfs_feed_task(entry.id)

    assert _retried() == [entry.id]
