"""Replacing the OSM extract from a URL (admin panel)."""

import contextlib
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from data_manager.net_security import OutboundURLBlocked, ResponseTooLarge
from planner import osm, pipeline
from planner.models import OsmDownload

pytestmark = pytest.mark.django_db

URL = 'https://download.example.org/poland-latest.osm.pbf'
HEADER = {
    'file': {'format': 'PBF'},
    'header': {
        'boxes': [[14.07, 49.0, 24.15, 54.84]],
        'option': {'osmosis_replication_timestamp': '2026-10-11T20:21:06Z'},
    },
}


@pytest.fixture
def admin(client_as, make_user):
    return client_as(make_user('Admin'))


@pytest.fixture
def current_osm(planner_data, settings):
    path = Path(pipeline.data_path(settings.PLANNER_OSM_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'old extract')
    return path


def fake_download(monkeypatch, *, body=b'new extract', status=200, error=None):
    @contextlib.contextmanager
    def download(url, *, into, **kwargs):
        if error:
            raise error
        into.write(body)

        class Response:
            status_code = status

        yield Response(), into

    monkeypatch.setattr(pipeline, 'safe_download', download)


def queued(url=URL):
    return OsmDownload.objects.create(url=url)


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


def test_admin_queues_a_download_and_only_one_at_a_time(
    admin, celery_send_task, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        first = admin.post('/api/planner/admin/osm/', {'url': URL}, format='json')
    second = admin.post('/api/planner/admin/osm/', {'url': URL}, format='json')

    assert first.status_code == 202
    assert (first.data['status'], first.data['url']) == ('queued', URL)
    assert celery_send_task.call_args.args[0] == 'planner.download_osm'
    assert (second.status_code, second.data['code']) == (409, 'planner.osm_download_in_progress')
    listed = admin.get('/api/planner/admin/osm/').data
    assert [row['id'] for row in listed['results']] == [first.data['id']]


def test_a_download_whose_worker_died_does_not_block_the_next(admin, celery_send_task):
    stuck = OsmDownload.objects.create(
        url=URL,
        status=OsmDownload.STATUS_RUNNING,
        started_at=timezone.now() - timedelta(hours=2),
    )

    response = admin.post('/api/planner/admin/osm/', {'url': URL}, format='json')

    stuck.refresh_from_db()
    assert response.status_code == 202
    assert (stuck.status, stuck.error_code) == ('failed', 'planner.osm_download_interrupted')


@pytest.mark.parametrize('role', ['Helper', 'DataProvider', None])
def test_only_admins_replace_the_map(client_as, make_user, role):
    response = client_as(make_user(role)).post('/api/planner/admin/osm/', {'url': URL})

    assert response.status_code in (401, 403)
    assert not OsmDownload.objects.exists()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def test_a_valid_extract_replaces_the_current_one(monkeypatch, current_osm):
    fake_download(monkeypatch)
    monkeypatch.setattr(osm, 'pbf_header', lambda path: HEADER)
    download = queued()

    pipeline.download_osm(download.id)

    download.refresh_from_db()
    assert current_osm.read_bytes() == b'new extract'
    assert (download.status, download.error_code, download.size_bytes) == ('succeeded', '', 11)
    assert download.data_timestamp.isoformat() == '2026-10-11T20:21:06+00:00'
    assert not Path(f'{current_osm}.download').exists()


@pytest.mark.parametrize(
    ('setup', 'code'),
    [
        ({'status': 404}, 'planner.osm_download_failed'),
        ({'error': ResponseTooLarge(4)}, 'planner.osm_too_large'),
        ({'error': OutboundURLBlocked('private address')}, 'planner.osm_url_blocked'),
        ({'error': OSError('disk full')}, 'planner.osm_download_failed'),
        ({'header_error': True}, 'planner.osm_invalid'),
    ],
)
def test_a_failed_download_keeps_the_current_extract(monkeypatch, current_osm, setup, code):
    header_error = setup.pop('header_error', False)
    fake_download(monkeypatch, **setup)

    def header(path):
        if header_error:
            raise osm.OsmPreparationError('not a PBF')
        return HEADER

    monkeypatch.setattr(osm, 'pbf_header', header)
    download = queued()

    pipeline.download_osm(download.id)

    download.refresh_from_db()
    assert (download.status, download.error_code) == ('failed', code)
    assert current_osm.read_bytes() == b'old extract'
    assert not Path(f'{current_osm}.download').exists()


def test_a_download_runs_once(monkeypatch, current_osm):
    fake_download(monkeypatch)
    monkeypatch.setattr(osm, 'pbf_header', lambda path: HEADER)
    download = queued()
    pipeline.download_osm(download.id)
    fake_download(monkeypatch, body=b'again')

    pipeline.download_osm(download.id)  # a retried task finds it no longer queued

    assert current_osm.read_bytes() == b'new extract'


def test_osmium_reads_the_header(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(HEADER))

    monkeypatch.setattr(osm.subprocess, 'run', run)

    assert osm.pbf_header('/data/osm/x.pbf') == HEADER
    assert calls == [['osmium', 'fileinfo', '--json', '--input-format', 'pbf', '/data/osm/x.pbf']]


@pytest.mark.parametrize(
    ('result', 'stdout'),
    [(1, ''), (0, '{"file": {"format": "XML"}, "header": {"boxes": [[1, 2, 3, 4]]}}')],
)
def test_anything_but_a_pbf_extract_is_rejected(monkeypatch, result, stdout):
    monkeypatch.setattr(
        osm.subprocess,
        'run',
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, result, stdout=stdout, stderr='bad'
        ),
    )

    with pytest.raises(osm.OsmPreparationError):
        osm.pbf_header('/data/osm/x.pbf')
