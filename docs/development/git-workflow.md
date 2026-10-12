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

## Enforcement

- **Locally (Claude Code):** `.claude/hooks/protect-main.sh` blocks `git commit` on `main`/`master`, and any `git push` that targets them. The hook needs `jq`.
- **CI:** `.github/workflows/ci.yml` runs the checks on every pull request and on every push to `main`.
- **On GitHub (to set up by a repository admin):** a branch protection rule or ruleset on `main` that requires a pull request, blocks force pushes, and requires the CI jobs to pass. This is the real guarantee; the hook only protects Claude sessions.
