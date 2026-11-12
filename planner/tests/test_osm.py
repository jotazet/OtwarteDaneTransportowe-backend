"""Cutting the OSM extract down with osmium: heartbeat callbacks and the timeout."""

import subprocess
import time

import pytest

from planner import osm

NEVER = 10**6


class FakeOsmium:
    """A Popen that exits with ``returncode`` after ``waits`` polls of a few ms each."""

    def __init__(self, waits=NEVER, returncode=0):
        self.waits = waits
        self.exit_code = returncode
        self.killed = False

    def wait(self, timeout=None):
        if self.killed or not self.waits:
            return self.poll()
        self.waits -= 1
        time.sleep(0.005)
        raise subprocess.TimeoutExpired('osmium', timeout)

    def poll(self):
        return -9 if self.killed else (None if self.waits else self.exit_code)

    def kill(self):
        self.killed = True


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
    started = popen()
    monkeypatch.setattr(osm, 'OSMIUM_TIMEOUT_SECONDS', 0.05)
    ticks = []

    with pytest.raises(osm.OsmPreparationError, match='timed out'):
        osm._osmium('tags-filter', 'in.pbf', '-o', 'out.pbf', on_tick=lambda: ticks.append(1))

    assert started[0][1].killed
    assert ticks


def test_osmium_is_killed_when_the_heartbeat_stops_the_build(popen):
    started = popen()

    def stop():
        raise RuntimeError('build aborted')

    with pytest.raises(RuntimeError):
        osm._osmium('tags-filter', 'in.pbf', '-o', 'out.pbf', on_tick=stop)

    assert started[0][1].killed


def test_failing_osmium_raises(popen):
    popen(waits=0, returncode=1)

    with pytest.raises(osm.OsmPreparationError, match='exited with 1'):
        osm._osmium('tags-filter', 'in.pbf', '-o', 'out.pbf')
