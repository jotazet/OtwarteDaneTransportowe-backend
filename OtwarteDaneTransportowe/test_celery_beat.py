import pytest
from django_celery_beat.schedulers import DatabaseScheduler

from OtwarteDaneTransportowe import celery_beat


def test_tick_touches_the_heartbeat_file_and_returns_the_interval(monkeypatch, tmp_path):
    heartbeat = tmp_path / 'heartbeat'
    monkeypatch.setattr(celery_beat, 'HEARTBEAT_FILE', heartbeat)
    monkeypatch.setattr(DatabaseScheduler, 'tick', lambda self: 5.0, raising=False)
    scheduler = object.__new__(celery_beat.HeartbeatScheduler)  # no broker or database

    assert scheduler.tick() == 5.0
    assert heartbeat.exists()


def test_a_failed_tick_leaves_the_heartbeat_stale(monkeypatch, tmp_path):
    heartbeat = tmp_path / 'heartbeat'
    monkeypatch.setattr(celery_beat, 'HEARTBEAT_FILE', heartbeat)

    def fail(self):
        raise RuntimeError('database is down')

    monkeypatch.setattr(DatabaseScheduler, 'tick', fail, raising=False)
    scheduler = object.__new__(celery_beat.HeartbeatScheduler)

    with pytest.raises(RuntimeError):
        scheduler.tick()
    assert not heartbeat.exists()
