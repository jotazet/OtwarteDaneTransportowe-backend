"""docker-compose.yml with docker-compose.prod.yml, as `docker compose config` resolves them."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROD = 'OtwarteDaneTransportowe.settings_prod'


def config(*files):
    result = subprocess.run(
        [
            'docker',
            'compose',
            *(f'--file={ROOT / name}' for name in files),
            'config',
            '--format',
            'json',
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        # Every profile, so that no service escapes the check.
        env={**os.environ, 'COMPOSE_PROFILES': '*'},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)['services']


@pytest.mark.skipif(shutil.which('docker') is None, reason='needs Docker Compose')
def test_every_app_service_runs_the_production_settings():
    """.env.example (an env_file) sets the development settings; the overlay must win."""
    services = config('docker-compose.yml', 'docker-compose.prod.yml')
    app = {name: service for name, service in services.items() if 'build' in service}

    assert {
        'web',
        'migrate',
        'celery_worker',
        'celery_realtime_worker',
        'celery_planner_worker',
    } <= set(app)
    assert {
        name: s['environment']['DJANGO_SETTINGS_MODULE'] for name, s in app.items()
    } == dict.fromkeys(app, PROD)
