"""scripts/deploy/envfile.py: the production .env the deploy job writes and the server checks."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.deploy import envfile

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'envfile.py'
SECRETS = {
    'DJANGO_SECRET_KEY': 'k+/=$HOME#x',
    'POSTGRES_PASSWORD': 'p1',
    'FEED_AUTH_ENCRYPTION_KEY': 'f1',
    'REDIS_PASSWORD': 'r1',
}
# The variables a release cannot do without (envfile.REQUIRED_VARIABLES).
REQUIRED = {name: 'set' for name in envfile.REQUIRED_VARIABLES}
# Values that break naive quoting: $, quotes, a trailing backslash, #, spaces, regexes.
TRICKY = {
    'CORS_URLS_REGEX': '^/(api|feed)/.*$',
    'A_QUOTES': 'say "hi" and \'bye\'',
    'A_BACKSLASH': 'ends with \\',
    'A_HASH': 'value # not a comment',
    'A_INTERPOLATION': '${HOME} and $$',
}


def run(*args, vars_json=None, secrets=SECRETS):
    env = {'PATH': '/usr/bin:/bin', **secrets}
    if vars_json is not None:
        env['VARS_JSON'] = json.dumps(vars_json)
    return subprocess.run(
        [sys.executable, '-I', str(SCRIPT), *args], env=env, capture_output=True, text=True
    )


def write(tmp_path, variables, secrets=SECRETS):
    path = tmp_path / 'production.env'
    result = run('write', str(path), vars_json={**REQUIRED, **variables}, secrets=secrets)
    return path, result


def parse(path):
    return envfile.parse(path.read_text())


def test_write_variables_then_secrets(tmp_path):
    path, result = write(
        tmp_path,
        {'DJANGO_ALLOWED_HOSTS': 'api.example.org', 'DEPLOY_HOST': '192.0.2.1'},
        {**SECRETS, 'PLANNER_INTERNAL_TOKEN': 't1'},
    )

    assert result.returncode == 0, result.stdout
    variables = {**REQUIRED, 'DJANGO_ALLOWED_HOSTS': 'api.example.org'}
    assert path.read_text() == ''.join(f'{k}="{v}"\n' for k, v in sorted(variables.items())) + (
        'DJANGO_SECRET_KEY="k+/=$$HOME#x"\n'
        'POSTGRES_PASSWORD="p1"\n'
        'FEED_AUTH_ENCRYPTION_KEY="f1"\n'
        'REDIS_PASSWORD="r1"\n'
        'PLANNER_INTERNAL_TOKEN="t1"\n'
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_round_trips_tricky_values(tmp_path):
    path, result = write(tmp_path, TRICKY)

    assert result.returncode == 0, result.stdout
    assert parse(path) == {**REQUIRED, **TRICKY, **SECRETS}


@pytest.mark.parametrize(
    ('variables', 'secrets', 'message'),
    [
        (
            {},
            {**SECRETS, 'DJANGO_SECRET_KEY': ''},
            'no secret DJANGO_SECRET_KEY',
        ),
        ({}, {**SECRETS, 'REDIS_PASSWORD': ''}, 'no secret REDIS_PASSWORD'),
        ({}, {**SECRETS, 'REDIS_PASSWORD': 'a@b'}, 'only letters, digits'),
        ({'CORS_ALLOWED_ORIGINS': ''}, SECRETS, 'no variable CORS_ALLOWED_ORIGINS'),
        ({'POSTGRES_PASSWORD': 'p2'}, SECRETS, 'must be a secret'),
        ({'MULTI': 'a\nb'}, SECRETS, 'line break'),
    ],
)
def test_write_refuses(tmp_path, variables, secrets, message):
    path, result = write(tmp_path, variables, secrets)

    assert result.returncode == 1
    assert message in result.stdout
    assert not path.exists()


@pytest.mark.parametrize(
    ('old', 'expected'),
    [
        # A hand-written .env as on the server before the first release.
        ('POSTGRES_PASSWORD=p1\nFEED_AUTH_ENCRYPTION_KEY=f1  # never change\n', 0),
        ("export POSTGRES_PASSWORD='p1'\nFEED_AUTH_ENCRYPTION_KEY=\"f1\"  # keep\n", 0),
        ('POSTGRES_PASSWORD=p0\nFEED_AUTH_ENCRYPTION_KEY=f1\n', 1),
        ('POSTGRES_PASSWORD=p1\nFEED_AUTH_ENCRYPTION_KEY=f0\n', 1),
        # Not set before (development fallback): setting it now is allowed.
        ('POSTGRES_PASSWORD=p1\n', 0),
    ],
)
def test_check_pinned_secrets(tmp_path, old, expected):
    new, _ = write(tmp_path, {})
    (tmp_path / 'old.env').write_text(old)

    result = run('check', str(tmp_path / 'old.env'), str(new))

    assert result.returncode == expected, result.stdout


def test_check_passes_without_an_old_env(tmp_path):
    new, _ = write(tmp_path, {})

    assert run('check', str(tmp_path / 'missing.env'), str(new)).returncode == 0


@pytest.mark.skipif(shutil.which('docker') is None, reason='needs Docker Compose')
def test_compose_reads_the_exact_values(tmp_path):
    """The values reach a container through env_file exactly as written."""
    write(tmp_path, TRICKY)
    (tmp_path / 'compose.yml').write_text(
        'services:\n  app:\n    image: scratch\n    env_file: production.env\n'
    )

    result = subprocess.run(
        ['docker', 'compose', '-f', str(tmp_path / 'compose.yml'), 'config', '--format', 'json'],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    environment = json.loads(result.stdout)['services']['app']['environment']
    # `config` prints a Compose file, so it escapes every $ of a value as $$ again.
    assert {k: v.replace('$$', '$') for k, v in environment.items()} == {
        **REQUIRED,
        **TRICKY,
        **SECRETS,
    }
