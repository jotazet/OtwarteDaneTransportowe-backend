# 0013. Deploy release tags from GitHub Actions over SSH

- **Status:** Proposed
- **Date:** 2026-11-11

## Context

Production was updated by hand: log in as the server's admin user, `git pull`, rebuild,
recreate. The steps lived in notes, backups before a migration depended on memory, and the
base Compose file bind-mounts the checkout over `/app`, so between the pull and the restart
running Celery workers imported the next release's modules (a burst of `FeedFetchError`
on every deployment). The production `.env` existed only on the server.

The repository is public and owned by a personal account. The server has a public IP and
runs other projects and a memory-hungry OpenTripPlanner.

## Decision

- **A release is a `vX.Y.Z` tag on `main`**, usually created as a GitHub Release. The CI
  workflow runs every check on the tag, then the `deploy` job deploys it. Nothing else
  deploys: not a push to `main`, not a tag outside `main` (checked by the job and again on
  the server).
- **The job connects over SSH as a dedicated `deploy` user** whose key has a forced command,
  `/usr/local/sbin/odt-deploy` (root-owned, installed once from `scripts/deploy/`), run
  through sudo as the user that owns the checkout. The key can pass a tag and a `.env`,
  nothing else: no shell, no forwarding.
- **The server builds the image from the checked-out tag** (`scripts/deploy/release.sh`):
  `.env` check, database dump, new `.env`, `docker compose build --pull`, `up -d` with
  migrations first, then the API must answer. The production Compose overlay runs the code
  from the image and mounts only `uploaded_data/` and `planner_data/`.
- **The production configuration lives in the GitHub environment `production`**: settings
  as variables, the four secrets by name. The job writes `.env` from them
  (`scripts/deploy/envfile.py`); the environment hands them only to `v*` tags.
  `FEED_AUTH_ENCRYPTION_KEY` and `POSTGRES_PASSWORD` cannot change through GitHub alone:
  the server refuses a `.env` that changes them.
- There is no approval step: creating the release is the deliberate act.

## Consequences

- Deploying is creating a release; every deployment has a log in Actions and a dump in
  `~/backups/release-*` (the newest ten).
- The server's `.env` is generated. A hand edit is overwritten by the next release; settings
  change in GitHub, without review or history in the repository, and redeploying an old tag
  uses today's settings.
- The image is built on the production host (a minute or two, little memory). A release
  restarts the planner worker, so it must not run during a graph build.
- Rolling back across a migration still means restoring the dump; a release does not
  reverse migrations.
- `odt-deploy` changes need a manual reinstall on the server; it is kept minimal for that
  reason.

## Alternatives considered

- **Self-hosted runner on the server** (as in other team projects behind a VPN) — no open
  port or key in GitHub, but GitHub advises against self-hosted runners for public
  repositories: a pull request from a fork can run code on them, and a personal account
  has no runner groups to restrict which workflows may use one.
- **Images built in Actions and pulled from GHCR** — no build on the host and faster
  rollback, but a registry login on the server and more moving parts. Still open; the
  production overlay already names the image `odt-backend:latest`.
- **The whole `.env` as one secret** — simpler, but no value can be seen or changed on its
  own.
- **Deploying every push to `main`** — not everything merged is meant to go out at once,
  and a tag records what is deployed.
