import io
import json
import zipfile
from pathlib import Path

import pytest
from django.core.files.base import ContentFile

from cases.models import TransportOrganization
from data_manager.models import FeedSubmission, FeedSubmissionHistory, StaticFeedEntry

# Re-exported fixture: SSRF-checked URLs resolve without real DNS.
from data_manager.tests.conftest import fake_dns  # noqa: F401
from planner import osm, otp_runtime, pipeline
from planner.models import PlannerFeed

GTFS_ZIP = (Path(__file__).parents[2] / 'data_manager' / 'tests' / 'GTFS_correct.zip').read_bytes()


def gtfs_variant(marker: str) -> bytes:
    """A valid GTFS archive whose bytes (and so sha256) differ per marker."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(GTFS_ZIP)) as src, zipfile.ZipFile(buffer, 'w') as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr('marker.txt', marker)
    return buffer.getvalue()


@pytest.fixture
def planner_data(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / 'media')
    data = tmp_path / 'planner'
    settings.PLANNER_DATA_DIR = settings.HOST_PLANNER_DATA_DIR = str(data)
    (data / 'osm').mkdir(parents=True)
    (data / 'osm' / 'region.osm.pbf').write_bytes(b'osm')
    settings.PLANNER_OSM_FILE = 'osm/region.osm.pbf'
    return data


def publish(submission):
    FeedSubmissionHistory.objects.create(
        submission=submission,
        event_type=FeedSubmissionHistory.EVENT_COMPLETED,
        stage_before=3,
        stage_after=4,
    )


@pytest.fixture
def make_submission(planner_data):
    def make(name='krk', *, org=None, published=True, content=GTFS_ZIP, data_type='gtfs'):
        org = org or TransportOrganization.objects.create(
            region='Region', transport_organization=f'Org {name}'
        )
        submission = FeedSubmission.objects.create(
            transport_organization=org, data_type=data_type, name=name
        )
        entry = StaticFeedEntry.objects.create(
            submission=submission,
            url=f'https://example.org/{name}.zip',
            hide_original=True,
            download_time_1='03:00',
        )
        entry.cached_file.save(f'{name}.zip', ContentFile(content), save=True)
        if published:
            publish(submission)
        return submission

    return make


@pytest.fixture
def make_feed(make_submission):
    def make(feed_id='krk', **kwargs):
        feed_kwargs = {
            key: kwargs.pop(key)
            for key in ('failure_policy', 'include_realtime', 'is_enabled')
            if key in kwargs
        }
        submission = make_submission(feed_id, **kwargs)
        return PlannerFeed.objects.create(submission=submission, feed_id=feed_id, **feed_kwargs)

    return make


def set_feed_content(feed, content: bytes):
    entry = StaticFeedEntry.objects.get(submission=feed.submission)
    entry.cached_file.save(f'{feed.feed_id}-new.zip', ContentFile(content), save=True)


class FakeOtp:
    """Stands in for Docker and the serving OTP; records what the pipeline did."""

    def __init__(self):
        self.runs = []
        self.events = []
        self.running = True
        self.healthy = True
        self.failing_feeds = {}  # feed_id -> (exit_code, log) for single-feed tests
        self.street_result = (0, '')
        self.graph_result = (0, '')
        self.served_feeds = set()

    def run_otp(
        self, host_dir, args, *, heap, mem_limit, memswap_limit=None, mounts=None, on_tick=None
    ):
        if on_tick:
            on_tick()
        config = json.loads(Path(host_dir, 'build-config.json').read_text())
        feed_ids = [feed['feedId'] for feed in config['transitFeeds']]
        if args == ['--buildStreet']:
            kind, (code, log), output = 'street', self.street_result, 'streetGraph.obj'
        elif args == ['--build', '--save']:
            kind, output = 'test', 'graph.obj'
            code, log = self.failing_feeds.get(feed_ids[0], (0, ''))
        else:
            kind, (code, log), output = 'graph', self.graph_result, 'graph.obj'
        self.runs.append(
            {
                'kind': kind,
                'args': args,
                'feeds': feed_ids,
                'mounts': mounts,
                'heap': heap,
                'mem_limit': mem_limit,
                'memswap_limit': memswap_limit,
            }
        )
        if code == 0:
            Path(host_dir, output).write_text(json.dumps(feed_ids))
        return otp_runtime.RunResult(exit_code=code, oom=False, log_tail=log)

    def stop_server(self):
        self.running = False
        self.events.append('stop')

    def start_server(self):
        self.running = True
        self.events.append('start')
        serving = Path(pipeline.data_path('serving'), 'graph.obj')
        self.served_feeds = set(json.loads(serving.read_text())) if serving.exists() else set()

    def server_running(self):
        return self.running

    def is_healthy(self):
        return self.healthy

    def query(self, document, variables=None, timeout=None):
        # Enough for the activation check and the search index (one agency per feed).
        return {
            'feeds': [
                {'feedId': feed_id, 'agencies': [{'gtfsId': f'{feed_id}:1', 'name': feed_id}]}
                for feed_id in sorted(self.served_feeds)
            ],
            'routes': [],
            'stops': [],
        }

    def remove_build_containers(self):
        return 0

    def kinds(self):
        return [run['kind'] for run in self.runs]


@pytest.fixture
def fake_otp(monkeypatch):
    fake = FakeOtp()
    for name in (
        'run_otp',
        'stop_server',
        'start_server',
        'server_running',
        'is_healthy',
        'query',
        'remove_build_containers',
    ):
        monkeypatch.setattr(otp_runtime, name, getattr(fake, name))
    monkeypatch.setattr(pipeline, '_sleep', lambda seconds: None)
    monkeypatch.setattr(pipeline, 'SERVER_START_TIMEOUT_SECONDS', 0)

    def prepare_osm(source, cells, target):
        Path(target).write_text('x')

    monkeypatch.setattr(osm, 'prepare_osm', prepare_osm)
    return fake
