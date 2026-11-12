import pytest
from django_celery_beat.schedulers import DatabaseScheduler

from OtwarteDaneTransportowe import celery_beat


@pytest.fixture
def heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(celery_beat, 'HEARTBEAT_FILE', tmp_path / 'heartbeat')
    return tmp_path / 'heartbeat'


# object.__new__: a scheduler without a broker or database.
def test_tick_touches_the_heartbeat_file_and_returns_the_interval(monkeypatch, heartbeat):
    monkeypatch.setattr(DatabaseScheduler, 'tick', lambda self: 5.0, raising=False)
    scheduler = object.__new__(celery_beat.HeartbeatScheduler)

    assert scheduler.tick() == 5.0
    assert heartbeat.exists()


def test_a_failed_tick_leaves_the_heartbeat_stale(monkeypatch, heartbeat):
    def fail(self):
        raise RuntimeError('database is down')

    monkeypatch.setattr(DatabaseScheduler, 'tick', fail, raising=False)
    scheduler = object.__new__(celery_beat.HeartbeatScheduler)

    with pytest.raises(RuntimeError):
        scheduler.tick()
    assert not heartbeat.exists()
