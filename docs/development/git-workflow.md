# Git workflow

Applies to both ODT repositories: this backend and [the frontend](https://github.com/jotazet/otwarte-dane-transportowe-frontend).

## Branches

- `main` is always deployable. **No direct commits, no direct pushes.** Changes land only through pull requests.
- Each feature or fix gets its own short-lived branch from `main`, named `<type>/<short-kebab-summary>`:

| Type | Use for | Example |
| --- | --- | --- |
| `feature/` | new user-facing capability | `feature/netex-feed-upload` |
| `fix/` | bug fix | `fix/realtime-proxy-timeout` |
| `perf/` | performance work without behaviour change | `perf/feeds-worker-single-process` |
| `refactor/` | restructuring without behaviour change | `refactor/submission-services` |
| `docs/` | documentation only | `docs/api-feeds` |
| `chore/` | tooling, dependencies, configuration | `chore/upgrade-django` |
| `test/` | tests only | `test/gtfs-validation-flow` |

- One branch covers one coherent change. If the scope grows, split it.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <imperative summary, max 72 chars>

<body: why the change was needed, when it isn't obvious from the summary>
```

- Allowed types match the branch types (`feat` may be used for `feature/` work), plus `build` (dependencies, Docker images), `ci` and `style` (formatting only).
- Scopes are optional and name the area: `blog`, `cases`, `feeds`, `realtime`, `auth`, `docker`, ...
- Keep commits focused; don't mix formatting churn with logic changes.
- **No AI attribution.** No `Co-Authored-By: Claude …` trailers, and no "Generated with …" lines in commits or PR descriptions. `.claude/settings.json` disables Claude Code's defaults for this.

## Pull requests

- Open the PR against `main` and fill in `.github/pull_request_template.md`.
- Before requesting review:
  - all checks from `CLAUDE.md` (Commands) pass;
  - docs are updated, and a change to the API contract has an entry in [`docs/api/changelog.md`](../api/changelog.md);
  - `/simplify` and `/code-review` have been run.
- A change that needs both repositories gets a PR in each, linked to each other. Merge and deploy the backend first when the frontend depends on it.
- Squash-merge, using the PR title as a Conventional Commit summary. Exception: a PR whose commits are listed in `.git-blame-ignore-revs` is merged with a merge commit, so the hashes survive.
- Delete the branch after merging.

## Releases

- A release is an annotated `vX.Y.Z` tag on a commit of `main` ([Semantic Versioning](https://semver.org/)), usually created as a GitHub Release with generated notes.
- In this repository the tag deploys to production after CI passes ([ADR 0013](../architecture/decisions/0013-release-tags-deploy-from-a-server-runner.md), [deployment, section 20](../deployment.md#20-releases-automatic-deployment-from-github)). A tag outside `main` is refused.
- Never move or delete a published tag; release a new version instead.

## Enforcement

- **Locally (Claude Code):** `.claude/hooks/protect-main.sh` is a PreToolUse hook on the Bash tool. It needs `jq` and is tested in `scripts/tests/test_protect_main_hook.py`.
  - On `main`/`master` it denies `git commit`, `merge`, `cherry-pick`, `revert`, `rebase` and `am` (`--abort` and `--quit` pass), and a `git pull` that would merge: `--no-ff`, or a branch other than `main`/`master`. A plain or `--ff-only` pull passes.
  - Anywhere it denies a `git push` that would update `main`/`master`: an explicit refspec, `HEAD`/`@` or no refspec while on `main`, `--all`, `--mirror` and deletions.
  - It asks for confirmation before pushing a `v*` tag (`--tags` and `--follow-tags` included) and before `gh release create`, since a release tag deploys to production.
  - It is a guard for the command shapes it recognizes, not a guarantee. It does not see git run by scripts, aliases or `bash -c`, and it reads the branch from the working directory when the command starts (an earlier `git switch main` or `git checkout main` in the same command is taken into account; a `cd` is not).
- **CI:** `.github/workflows/ci.yml` runs the checks on every pull request, on every push to `main` and on release tags, where it also deploys. Dependabot proposes updates of actions, Python packages and the base image monthly.
- **On GitHub: not set up.** As of 2026-11-12 `main` has no branch protection rule and the repository has no rulesets, so nothing on GitHub stops a direct push to `main`. A repository admin should add a rule or ruleset on `main` that requires a pull request, blocks force pushes and requires the CI jobs to pass. That would be the real guarantee; the hook only protects Claude sessions.
