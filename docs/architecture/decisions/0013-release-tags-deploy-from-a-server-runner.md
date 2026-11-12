# 0013. Deploy release tags from a GitHub Actions runner on the server

- **Status:** Proposed
- **Date:** 2026-11-11

## Context

Production was updated by hand: log in as the server's admin user, `git pull`, rebuild,
recreate. The steps lived in notes, backups before a migration depended on memory, and the
base Compose file bind-mounts the checkout over `/app`, so between the pull and the restart
running Celery workers imported the next release's modules (a burst of `FeedFetchError`
on every deployment). The production `.env` existed only on the server.

The repository is public and owned by a personal account; only its owner can push tags.
The server runs the backend, the frontend, other projects and a memory-hungry
OpenTripPlanner.

## Decision

- **A release is a `vX.Y.Z` tag on `main`**, usually created as a GitHub Release. The CI
  workflow runs every check on the tag, then the `deploy` job deploys it. Nothing else
  deploys: not a push to `main`, not a tag outside `main` (checked on the server).
- **The `deploy` job runs on a self-hosted runner on the production server** (label
  `odt-production`), as the system user `odt-runner`. That user is not in the `docker`
  group and may run exactly one command through sudo: `/usr/local/sbin/odt-deploy`
  (root-owned, installed once from `scripts/deploy/`) as the user that owns the checkout.
  Every other job runs on GitHub-hosted runners. ODT's runners (backend now, frontend
  later) share `odt-runner`, one runner directory per repository under
  `/opt/actions-runner/`.
- **Workflows from fork pull requests need the owner's approval** for every outside
  contributor (repository setting), because a pull request can change `ci.yml` and point a
  job at the runner.
- **The server builds the image from the checked-out tag** (`scripts/deploy/release.sh`):
  `.env` check, `docker compose build --pull`, database dump, new `.env`, `up -d` with
  migrations first, then the API must answer. The production Compose overlay runs the code
  from the image and mounts only `uploaded_data/` and `planner_data/`.
- **The production configuration lives in the GitHub environment `production`**: settings
  as variables, the four secrets by name. The job writes `.env` from them
  (`scripts/deploy/envfile.py`); the environment hands them only to `v*` tags.
- **A release sets only application settings and secrets.** `odt-deploy` refuses a `.env`
  with any key outside the list in `envfile.py` (taken from `main`, so an older tag is
  restricted too) and adds the server's root-owned `/etc/odt/backend-host.env`, which wins:
  user ids, host paths, images, Compose settings and interpolated values, resource limits,
  internal addresses and `DJANGO_SETTINGS_MODULE` are set there, not in GitHub.
  `FEED_AUTH_ENCRYPTION_KEY` and `POSTGRES_PASSWORD` cannot change through GitHub alone:
  the server refuses a `.env` that changes them.
- There is no approval step: creating the release is the deliberate act.

## Consequences

- Deploying is creating a release; every deployment has a log in Actions and a dump in
  `~/backups/release-*` (the newest ten). No inbound port or key is involved: the runner
  connects out to GitHub.
- The runner is persistent. Code that reaches it (an approved pull request with a changed
  workflow) runs as `odt-runner` and could stay there to read later deployments' secrets;
  it cannot use Docker directly, but it can run `odt-deploy` with any tag on `main`
  (an older one too) and a `.env` of its own, so it chooses the application settings and
  secrets of a deployment; it cannot change the host file, so not which images run, as
  which user or with which host mounts. The frontend's runner runs as the same user, so
  such code reaches both repositories' deployments; a user per repository would separate
  them. Approving a fork's workflow means reading its workflow changes first.
- The server's `.env` is generated. A hand edit is overwritten by the next release; settings
  change in GitHub, without review or history in the repository, and redeploying an old tag
  uses today's settings. Host settings change on the server as root, and a new application
  setting that releases should set needs adding to the list in `envfile.py`.
- The image is built on the production host (a minute or two, little memory). A release
  restarts the planner worker, so it refuses to start while a graph build runs; it prunes
  the host's build cache older than a week.
- Rolling back across a migration still means restoring the dump; a release does not
  reverse migrations.
- `odt-deploy` changes need a manual reinstall on the server; it is kept minimal for that
  reason. The runner updates itself.

## Alternatives considered

- **SSH from a GitHub-hosted runner** to a `deploy` user whose key has `odt-deploy` as a
  forced command (the first version of this decision) — nothing runs permanently on the
  server and a pull request cannot reach it, but a private key with access to production
  lives in GitHub and the SSH port must accept connections from GitHub's address ranges.
  The owner preferred the runner, as in other team projects.
- **Images built in Actions and pulled from GHCR** — no build on the host and faster
  rollback, but a registry login on the server and more moving parts. Still open; the
  production overlay already names the image `odt-backend:latest`.
- **The whole `.env` as one secret** — simpler, but no value can be seen or changed on its
  own.
- **Deploying every push to `main`** — not everything merged is meant to go out at once,
  and a tag records what is deployed.
