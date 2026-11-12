"""The beat scheduler, with a heartbeat file for the celery_beat healthcheck (docker-compose.yml)."""

from pathlib import Path

from django_celery_beat.schedulers import DatabaseScheduler

HEARTBEAT_FILE = Path('/tmp/celery-beat-heartbeat')


class HeartbeatScheduler(DatabaseScheduler):
    """DatabaseScheduler that touches HEARTBEAT_FILE after every tick (at least every 5 s),
    so a beat that hangs, for example on the database, stops refreshing it."""

    def tick(self, *args, **kwargs):
        interval = super().tick(*args, **kwargs)
        HEARTBEAT_FILE.touch()
        return interval
