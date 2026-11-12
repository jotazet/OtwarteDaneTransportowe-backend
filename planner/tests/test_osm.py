"""Cutting the OSM extract down with osmium: heartbeat callbacks and the timeout."""

import subprocess
import time

import pytest

from planner import osm


class FakeOsmium:
    """A Popen that finishes after ``waits`` polls (never, if None)."""

    def __init__(self, waits=None, returncode=0):
        self.waits = waits
        self.final_returncode = returncode
        self.returncode = None
        self.killed = False

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if self.waits == 0:
            self.returncode = self.final_returncode
            return self.returncode
        if self.waits is not None:
            self.waits -= 1
        time.sleep(min(timeout, 0.01))
        raise subprocess.TimeoutExpired('osmium', timeout)

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def popen(monkeypatch):
    started = []

    def install(**kwargs):
        def fake_popen(command, **popen_kwargs):
            process = FakeOsmium(**kwargs)
            started.append((command, process))
            return process

        monkeypatch.setattr(osm.subprocess, 'Popen', fake_popen)
        return started

    return install


def test_heartbeat_is_called_while_osmium_runs(popen, tmp_path):
    started = popen(waits=2)
    ticks = []

    osm.prepare_osm(
        '/data/region.osm.pbf',
        {(0, 0)},
        str(tmp_path / 'street.osm.pbf'),
        on_tick=lambda: ticks.append(1),
    )

    assert [command[1] for command, _ in started] == ['tags-filter', 'extract']
    assert len(ticks) == 4  # two polls in each of the two passes
    assert not (tmp_path / 'coverage.geojson').exists()


def test_osmium_is_killed_when_it_runs_past_the_timeout(popen, monkeypatch):
    started = popen(waits=None)
    monkeypatch.setattr(osm, 'OSMIUM_TIMEOUT_SECONDS', 0.05)
    monkeypatch.setattr(osm, 'OSMIUM_POLL_SECONDS', 0.01)
    ticks = []

    with pytest.raises(osm.OsmPreparationError, match='timed out'):
        osm._osmium('tags-filter', 'in.pbf', '-o', 'out.pbf', on_tick=lambda: ticks.append(1))

    assert started[0][1].killed
    assert ticks


def test_osmium_is_killed_when_the_heartbeat_stops_the_build(popen):
    started = popen(waits=None)

    def stop():
        raise RuntimeError('build aborted')

    with pytest.raises(RuntimeError):
        osm._osmium('tags-filter', 'in.pbf', '-o', 'out.pbf', on_tick=stop)

    assert started[0][1].killed


def test_failing_osmium_raises(popen):
    popen(waits=0, returncode=1)

    with pytest.raises(osm.OsmPreparationError, match='exited with 1'):
        osm._osmium('tags-filter', 'in.pbf', '-o', 'out.pbf')
