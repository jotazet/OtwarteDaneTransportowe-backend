"""scripts/deploy/release.sh, with docker, git and the Compose helper replaced by fakes."""

import shutil
import subprocess
from pathlib import Path

import pytest

RELEASE = Path(__file__).resolve().parents[1] / 'deploy' / 'release.sh'

# Each fake appends its arguments to calls.log. The fake docker keeps the odt-backend tags
# in tags (one per line) and the Compose helper fails `up --wait` when wait-fails exists.
FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$STATE/calls.log"
case "$1 $2" in
    "image tag") echo "${4#odt-backend:}" >> "$STATE/tags" ;;
    "image ls") cat "$STATE/tags" ;;
esac
"""
FAKE_COMPOSE = """#!/bin/sh
echo "compose $*" >> "$STATE/calls.log"
case "$*" in
    *--wait*) [ ! -e "$STATE/wait-fails" ] ;;
esac
"""
FAKE_GIT = '#!/bin/sh\necho 0123abc\n'


@pytest.fixture
def sandbox(tmp_path):
    """A checkout with release.sh and fake helpers, fake commands on PATH, and their state."""
    root = tmp_path / 'checkout'
    (root / 'scripts' / 'deploy').mkdir(parents=True)
    shutil.copy(RELEASE, root / 'scripts' / 'deploy' / 'release.sh')
    (root / 'scripts' / 'deploy' / 'envfile.py').write_text('')
    (root / 'scripts' / 'deploy.sh').write_text(FAKE_COMPOSE)
    (root / 'scripts' / 'deploy.sh').chmod(0o755)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in {'docker': FAKE_DOCKER, 'git': FAKE_GIT}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp_path / 'home').mkdir()
    (tmp_path / 'new.env').write_text('A=1\n')
    return tmp_path


def release(sandbox, label='v1.4.0', tags=()):
    (sandbox / 'tags').write_text(''.join(f'{tag}\n' for tag in tags))
    env = {
        'PATH': f'{sandbox / "bin"}:/usr/bin:/bin',
        'HOME': str(sandbox / 'home'),
        'STATE': str(sandbox),
    }
    result = subprocess.run(
        [
            'sh',
            str(sandbox / 'checkout' / 'scripts' / 'deploy' / 'release.sh'),
            label,
            str(sandbox / 'new.env'),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    log = sandbox / 'calls.log'
    return result, log.read_text().splitlines() if log.exists() else []


def test_a_release_waits_for_every_service_and_tags_its_image(sandbox):
    result, calls = release(sandbox)

    assert result.returncode == 0, result.stderr
    assert 'compose up -d --wait --wait-timeout 900' in calls
    assert 'docker image tag odt-backend:latest odt-backend:v1.4.0' in calls
    assert 'release: v1.4.0 is live' in result.stdout


def test_an_unhealthy_service_fails_the_release_without_tagging_its_image(sandbox):
    (sandbox / 'wait-fails').touch()

    result, calls = release(sandbox, tags=['latest', 'v1.3.0'])

    assert result.returncode == 1
    assert 'not every service became healthy' in result.stderr
    assert 'compose ps -a' in calls
    assert not any(call.startswith('docker image tag') for call in calls)
    assert not any(call.startswith('docker image rm') for call in calls)
    assert (sandbox / 'tags').read_text() == 'latest\nv1.3.0\n'


def test_only_the_newest_release_images_are_kept(sandbox):
    result, calls = release(
        sandbox,
        label='v1.10.0',
        tags=['latest', 'previous', 'pre-main', 'v1.2.0', 'v1.9.0', 'v1.8.1'],
    )

    assert result.returncode == 0, result.stderr
    # v1.10.0, v1.9.0 and v1.8.1 stay; tags not of a release are never removed.
    assert [call for call in calls if call.startswith('docker image rm')] == [
        'docker image rm odt-backend:v1.2.0'
    ]


@pytest.mark.parametrize('label', ['', 'main', 'v1.4', 'v1.4.0-rc1', 'v1.4.0;id'])
def test_a_label_that_is_not_a_release_tag_is_refused_before_anything_runs(sandbox, label):
    result, calls = release(sandbox, label=label)

    assert result.returncode == 1
    assert 'expected a release tag vX.Y.Z' in result.stderr
    assert calls == []
