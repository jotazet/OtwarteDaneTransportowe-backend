"""scripts/deploy/release.sh, with docker and the Compose helper replaced by fakes."""

import shutil
import subprocess
from pathlib import Path

import pytest

RELEASE = Path(__file__).resolve().parents[1] / 'deploy' / 'release.sh'

# Each fake appends its arguments to calls.log. The fake docker keeps the release image's
# tags in tags (one per line) and the Compose helper fails `up --wait` when wait-fails
# exists and prints the image of the version a release writes into the new .env.
IMAGE = 'ghcr.io/jotazet/odt-backend'
FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$STATE/calls.log"
case "$1 $2" in
    "image ls") cat "$STATE/tags" ;;
esac
"""
FAKE_COMPOSE = f"""#!/bin/sh
echo "compose $*" >> "$STATE/calls.log"
case "$*" in
    *--wait*) [ ! -e "$STATE/wait-fails" ] ;;
    *"config --images"*) echo "{IMAGE}:$(sed -n 's/^ODT_VERSION=//p' "$2")" ;;
esac
"""


@pytest.fixture
def sandbox(tmp_path):
    """A deployment directory with release.sh and fake helpers, fake commands on PATH, and
    their state."""
    root = tmp_path / 'odt-backend'
    (root / 'scripts' / 'deploy').mkdir(parents=True)
    shutil.copy(RELEASE, root / 'scripts' / 'deploy' / 'release.sh')
    (root / 'scripts' / 'deploy' / 'envfile.py').write_text('')
    (root / 'scripts' / 'deploy.sh').write_text(FAKE_COMPOSE)
    (root / 'scripts' / 'deploy.sh').chmod(0o755)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'docker').write_text(FAKE_DOCKER)
    (bin_dir / 'docker').chmod(0o755)
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
            str(sandbox / 'odt-backend' / 'scripts' / 'deploy' / 'release.sh'),
            label,
            str(sandbox / 'new.env'),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    log = sandbox / 'calls.log'
    return result, log.read_text().splitlines() if log.exists() else []


def test_a_release_pulls_its_image_and_waits_for_every_service(sandbox):
    result, calls = release(sandbox)

    assert result.returncode == 0, result.stderr
    env_new = str(sandbox / 'new.env')
    assert calls[:2] == [
        'docker ps -q --filter label=odt.planner',
        f'compose --env-file {env_new} pull --quiet web',
    ]
    assert 'compose up -d --wait --wait-timeout 900' in calls
    assert 'release: v1.4.0 is live' in result.stdout
    # The installed .env runs the release's version.
    assert (sandbox / 'odt-backend' / '.env').read_text() == 'A=1\nODT_VERSION=v1.4.0\n'


def test_an_unhealthy_service_fails_the_release_and_keeps_every_image(sandbox):
    (sandbox / 'wait-fails').touch()

    result, calls = release(sandbox, tags=['v1.1.0', 'v1.2.0', 'v1.3.0', 'v1.4.0'])

    assert result.returncode == 1
    assert 'not every service became healthy' in result.stderr
    assert 'compose ps -a' in calls
    assert not any(call.startswith('docker image rm') for call in calls)


def test_only_the_newest_release_images_are_kept(sandbox):
    result, calls = release(
        sandbox,
        label='v1.10.0',
        tags=['main', 'sha-0123abc', 'v1.2.0', 'v1.9.0', 'v1.8.1', 'v1.10.0'],
    )

    assert result.returncode == 0, result.stderr
    # v1.10.0, v1.9.0 and v1.8.1 stay; tags not of a release are never removed.
    assert [call for call in calls if call.startswith('docker image rm')] == [
        f'docker image rm {IMAGE}:v1.2.0'
    ]


@pytest.mark.parametrize('label', ['', 'main', 'v1.4', 'v1.4.0-rc1', 'v1.4.0;id'])
def test_a_label_that_is_not_a_release_tag_is_refused_before_anything_runs(sandbox, label):
    result, calls = release(sandbox, label=label)

    assert result.returncode == 1
    assert 'expected a release tag vX.Y.Z' in result.stderr
    assert calls == []
