"""scripts/deploy/backup.sh: the scheduled backup, with Docker (and rclone) replaced by fakes."""

import fcntl
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
DUMP = 'PGDMP fake custom-format archive'

pytestmark = pytest.mark.skipif(shutil.which('rsync') is None, reason='needs rsync')

# Stands in for `docker compose exec -T postgres ...`: the dump prints $FAKE_DUMP (or fails
# when it is "fail"), pg_restore --list accepts only input that starts with PGDMP.
FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$*" in
    *pg_dump*) [ "$FAKE_DUMP" != fail ] || exit 1; printf %s "$FAKE_DUMP" ;;
    *'pg_restore --list'*) [ "$(head -c 5)" = PGDMP ] ;;
    *) exit 1 ;;
esac
"""
FAKE_RCLONE = """#!/bin/sh
echo "rclone $*" >> "$FAKE_LOG"
"""


@pytest.fixture
def checkout(tmp_path):
    """A checkout with the deploy scripts, data directories and fake commands on PATH."""
    root = tmp_path / 'checkout'
    (root / 'scripts' / 'deploy').mkdir(parents=True)
    shutil.copy(DEPLOY / 'deploy.sh', root / 'scripts' / 'deploy.sh')
    shutil.copy(DEPLOY / 'deploy' / 'backup.sh', root / 'scripts' / 'deploy' / 'backup.sh')
    files = {
        'uploaded_data/1/static/feed.zip': 'gtfs',
        'uploaded_data/1/validation/report.json': '{}',
        'uploaded_data/2/dynamic/cached/trip_updates/feed.pb': 'realtime cache',
        'uploaded_data/blog/images/a.png': 'png',
        'planner_data/snapshots/krakow.zip': 'snapshot',
        'planner_data/graphs/1/graph.obj': 'rebuildable',
    }
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for command, script in {'docker': FAKE_DOCKER, 'rclone': FAKE_RCLONE}.items():
        (bin_dir / command).write_text(script)
        (bin_dir / command).chmod(0o755)
    return root


@pytest.fixture
def backups(tmp_path):
    return tmp_path / 'backups'


def run(checkout, backups, dump=DUMP, **settings):
    env = {
        'PATH': f'{checkout.parent / "bin"}:/usr/bin:/bin',
        'HOME': str(checkout.parent),
        'FAKE_LOG': str(checkout.parent / 'calls.log'),
        'FAKE_DUMP': dump,
        'BACKUP_DIR': str(backups),
        **settings,
    }
    return subprocess.run(
        ['sh', str(checkout / 'scripts' / 'deploy' / 'backup.sh')],
        env=env,
        capture_output=True,
        text=True,
    )


def complete(backups):
    """The complete backups, named by their UTC start time."""
    return sorted(backups.glob('????????T??????Z'))


def make_old(backups, *names):
    for name in names:
        (backups / name).mkdir(parents=True)


def test_backs_up_the_database_and_the_data_that_cannot_be_recreated(checkout, backups):
    result = run(checkout, backups)

    assert result.returncode == 0, result.stderr
    assert sorted(os.listdir(backups)) == ['.lock', complete(backups)[0].name]
    backup = complete(backups)[0]
    assert (backup / 'db.dump').read_text() == DUMP
    copied = sorted(str(p.relative_to(backup)) for p in backup.rglob('*') if p.is_file())
    assert copied == [
        'complete',
        'db.dump',
        'planner_data/snapshots/krakow.zip',
        'uploaded_data/1/static/feed.zip',
        'uploaded_data/1/validation/report.json',
        'uploaded_data/blog/images/a.png',
    ]
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert 'WARNING: BACKUP_REMOTE is not set' in result.stderr
    # The variables are expanded in the container, not in this shell.
    calls = (checkout.parent / 'calls.log').read_text()
    assert 'sh -c pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' in calls


@pytest.mark.parametrize(
    ('dump', 'message'),
    [
        ('', 'pg_restore cannot read the database dump'),
        ('-- not a custom-format dump', 'pg_restore cannot read the database dump'),
        ('fail', 'FAILED (exit 1)'),
    ],
)
def test_a_failed_or_unusable_dump_fails_and_leaves_no_backup(checkout, backups, dump, message):
    make_old(backups, '20260101T000000Z')

    result = run(checkout, backups, dump=dump)

    assert result.returncode == 1
    assert message in result.stderr
    assert 'FAILED' in result.stderr
    assert sorted(os.listdir(backups)) == ['.lock', '20260101T000000Z']


def test_unchanged_files_are_hard_links_to_the_previous_backup(checkout, backups):
    assert run(checkout, backups).returncode == 0
    complete(backups)[0].rename(backups / '20260101T000000Z')
    (checkout / 'uploaded_data/1/static/feed.zip').write_text('a new version')
    # A scheduled download rewrites an unchanged feed: same content, new time.
    os.utime(checkout / 'planner_data/snapshots/krakow.zip', (1e9, 1e9))

    result = run(checkout, backups)

    assert result.returncode == 0, result.stderr
    old, new = complete(backups)
    snapshot = 'planner_data/snapshots/krakow.zip'
    assert (old / snapshot).stat().st_ino == (new / snapshot).stat().st_ino
    feed = 'uploaded_data/1/static/feed.zip'
    assert (old / feed).stat().st_ino != (new / feed).stat().st_ino
    assert (new / feed).read_text() == 'a new version'


def test_keeps_the_newest_backups_and_removes_leftovers(checkout, backups):
    make_old(backups, '20260101T000000Z', '20260102T000000Z', '20260103T000000Z')
    make_old(backups, '.partial-20260104T000000Z', 'release-notes')

    result = run(checkout, backups, BACKUP_KEEP='2')

    assert result.returncode == 0, result.stderr
    names = [p.name for p in complete(backups)]
    assert names[0] == '20260103T000000Z' and len(names) == 2
    assert sorted(os.listdir(backups)) == sorted([*names, '.lock', 'release-notes'])


def test_copies_the_new_backup_off_the_host_with_rsync(checkout, backups, tmp_path):
    remote = tmp_path / 'remote'
    remote.mkdir()
    assert run(checkout, backups, BACKUP_REMOTE=str(remote)).returncode == 0
    first = complete(backups)[0].name
    complete(backups)[0].rename(backups / '20260101T000000Z')
    (remote / first).rename(remote / '20260101T000000Z')

    result = run(checkout, backups, BACKUP_REMOTE=str(remote))

    assert result.returncode == 0, result.stderr
    new = complete(backups)[-1].name
    assert sorted(os.listdir(remote)) == ['20260101T000000Z', new]
    assert (remote / new / 'db.dump').read_text() == DUMP
    assert (remote / new / 'complete').exists()
    snapshot = 'planner_data/snapshots/krakow.zip'
    assert (remote / new / snapshot).stat().st_ino == (
        remote / '20260101T000000Z' / snapshot
    ).stat().st_ino


def test_copies_the_new_backup_off_the_host_with_rclone(checkout, backups):
    result = run(checkout, backups, BACKUP_REMOTE='store:odt', BACKUP_REMOTE_TOOL='rclone')

    assert result.returncode == 0, result.stderr
    backup = complete(backups)[0]
    calls = (checkout.parent / 'calls.log').read_text()
    assert calls.splitlines()[-2:] == [
        f'rclone copy --exclude /complete {backup} store:odt/{backup.name}',
        f'rclone copy {backup}/complete store:odt/{backup.name}',
    ]


def test_a_failed_off_host_copy_fails_but_keeps_the_local_backup(checkout, backups, tmp_path):
    result = run(checkout, backups, BACKUP_REMOTE=str(tmp_path / 'missing' / 'remote'))

    assert result.returncode != 0
    assert 'FAILED' in result.stderr
    assert len(complete(backups)) == 1


def test_without_uploaded_data_the_backup_fails(checkout, backups):
    shutil.rmtree(checkout / 'uploaded_data')

    result = run(checkout, backups)

    assert result.returncode == 1
    assert 'no uploaded_data/' in result.stderr
    assert complete(backups) == []


def test_a_second_run_fails_while_one_is_running(checkout, backups):
    backups.mkdir()
    with open(backups / '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run(checkout, backups)

    assert result.returncode == 1
    assert 'another backup is running' in result.stderr
    assert not (checkout.parent / 'calls.log').exists()


def test_without_the_planner_the_snapshots_are_skipped(checkout, backups):
    shutil.rmtree(checkout / 'planner_data')

    result = run(checkout, backups)

    assert result.returncode == 0, result.stderr
    assert 'no planner_data/snapshots/' in result.stderr
    assert not (complete(backups)[0] / 'planner_data').exists()


@pytest.mark.parametrize(
    'invalid',
    [
        {'BACKUP_DIR': 'backups'},
        {'BACKUP_KEEP': '0'},
        {'BACKUP_KEEP': 'seven'},
        {'BACKUP_REMOTE_TOOL': 'scp'},
    ],
)
def test_refuses_invalid_settings_before_doing_anything(checkout, backups, invalid):
    result = run(checkout, backups, **invalid)

    assert result.returncode == 2
    assert not backups.exists()
    assert not (checkout.parent / 'calls.log').exists()
