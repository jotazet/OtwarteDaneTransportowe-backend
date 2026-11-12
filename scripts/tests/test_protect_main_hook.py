""".claude/hooks/protect-main.sh: the PreToolUse guard for the git workflow."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / '.claude' / 'hooks' / 'protect-main.sh'

pytestmark = pytest.mark.skipif(
    shutil.which('jq') is None or shutil.which('git') is None, reason='needs jq and git'
)


@pytest.fixture(scope='module')
def repos(tmp_path_factory):
    """Two clones of one throwaway repository: one on main, one on a feature branch."""
    base = tmp_path_factory.mktemp('repos')
    main = base / 'main'
    git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
    subprocess.run([*git, 'init', '-q', '-b', 'main', str(main)], check=True)
    subprocess.run(
        [*git, '-C', str(main), 'commit', '-q', '--allow-empty', '-m', 'init'], check=True
    )
    feature = base / 'feature'
    subprocess.run(['git', 'clone', '-q', str(main), str(feature)], check=True)
    subprocess.run(['git', '-C', str(feature), 'switch', '-q', '-c', 'fix/x'], check=True)
    return {'main': main, 'feature': feature}


def decide(repos, branch, command):
    payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': str(repos[branch])}
    result = subprocess.run(
        ['bash', str(HOOK)], input=json.dumps(payload), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return 'allow'
    output = json.loads(result.stdout)['hookSpecificOutput']
    assert output['hookEventName'] == 'PreToolUse'
    assert output['permissionDecisionReason']
    return output['permissionDecision']


@pytest.mark.parametrize(
    'command',
    [
        'git commit -m x',
        'git -c user.name=x commit -m x',
        'git --no-pager commit -m x',
        'git -C . commit -m x',
        '/usr/bin/git commit -m x',
        'GIT_AUTHOR_NAME=x git commit -m x',
        'git add . && git commit -m "fix: a; b"',
        'git merge fix/x',
        'git cherry-pick abc123',
        'git revert HEAD',
        'git rebase origin/main',
        'git am < patch',
        'git merge --continue',
        'git pull --no-ff',
        'git pull origin fix/x',
        'git push',
        'git push -u origin',
        'git push origin HEAD',
        'git push origin @',
    ],
)
def test_denied_on_main(repos, command):
    assert decide(repos, 'main', command) == 'deny'


@pytest.mark.parametrize(
    'command',
    [
        'git status',
        'git pull',
        'git pull --ff-only',
        'git pull --rebase origin main',
        'git merge --abort',
        'git log --grep=commit',
        'git merge-base HEAD origin/main',
        'gh pr merge 12 --squash',
        'echo "git commit -m x"',
        "echo 'x; git merge y'",
        'git switch -c fix/y && git commit -m x',
        'git push origin fix/x',
    ],
)
def test_allowed_on_main(repos, command):
    assert decide(repos, 'main', command) == 'allow'


@pytest.mark.parametrize(
    'command',
    [
        'git commit -m x',
        'git merge origin/main',
        'git rebase origin/main',
        'git push',
        'git push -u origin HEAD',
        'git push origin fix/x',
        'git push --force-with-lease origin fix/x',
        'git commit -m "docs: say git push origin main"',
        'git log --grep=main',
    ],
)
def test_allowed_on_a_feature_branch(repos, command):
    assert decide(repos, 'feature', command) == 'allow'


@pytest.mark.parametrize(
    'command',
    [
        'git push origin main',
        'git push origin "main"',
        'git -c push.default=current push origin HEAD:main',
        'git push origin fix/x:refs/heads/main',
        'git push --force origin +HEAD:main',
        'git push origin :main',
        'git push origin --delete master',
        'git push --all',
        'git push --mirror',
        'git switch main && git merge fix/x',
        'git checkout main && git push',
    ],
)
def test_denied_on_a_feature_branch(repos, command):
    assert decide(repos, 'feature', command) == 'deny'


@pytest.mark.parametrize(
    'command',
    [
        'git push origin v1.2.3',
        'git push origin refs/tags/v1.2.3',
        'git push origin tag v1.2.3',
        'git push --tags',
        'git push --follow-tags origin fix/x',
        'gh release create v1.2.3 --generate-notes',
    ],
)
def test_release_tags_ask(repos, command):
    assert decide(repos, 'feature', command) == 'ask'


def test_deny_wins_over_ask(repos):
    assert decide(repos, 'feature', 'git push origin v1.2.3 main') == 'deny'


def test_ignores_other_tools(repos):
    payload = {'tool_name': 'Bash', 'tool_input': {}, 'cwd': str(repos['main'])}
    result = subprocess.run(
        ['bash', str(HOOK)], input=json.dumps(payload), capture_output=True, text=True
    )
    assert (result.returncode, result.stdout) == (0, '')
