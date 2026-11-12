# 0016. Build release images in CI, pull them from GHCR, keep no checkout on the server

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

[ADR 0013](0013-release-tags-deploy-from-a-server-runner.md) deploys a release tag from a
runner on the production server, which checked the tag out in a Git clone and built the
image there. That host runs the backend, OpenTripPlanner, the frontend and other projects
on 11 GB of RAM, usually with 10 GB in use; a build there competes with OTP, and the image
that ran in production was not the one CI had checked. The clone was also the deployment
directory, so the server kept a full working copy that nothing but releases may touch.

## Decision

- **CI builds the production image** (`INSTALL_DEV=false`) after every check passed, for
  every push to `main` (`:main`) and every tag (`:vX.Y.Z`, plus `:sha-<commit>`), and pushes
  it to GitHub's registry as `ghcr.io/jotazet/odt-backend`, with the workflow's own token.
- **The package is public**, like the repository: the server pulls without credentials.
  The image holds code and dependencies only; settings and secrets come from `.env`.
- **The server keeps a deployment directory, not a checkout:** `/srv/odt-backend`, owned by
  the system user `odt` (no login, no sudo, in the `docker` group), which `odt-deploy` runs
  as instead of the admin user; its home `/var/lib/odt` holds the release and daily
  backups. The directory holds `.env`,
  `uploaded_data/`, `planner_data/`, and the Compose files and deploy scripts of the running
  release. `odt-deploy` asks GitHub's API for the tag's commit, refuses it unless `main`
  contains it, downloads that commit's archive and copies `docker-compose*.yml`,
  `.env.example` and `scripts/deploy*` into the directory, then runs that release's
  `release.sh`, which pulls the tag's image instead of building it. `ODT_VERSION` in `.env`
  names the running version.
- The Compose project name is fixed in `docker-compose.prod.yml`, so the database volume and
  the networks do not depend on the directory's name.
- Everything else in ADR 0013 stands: tags deploy, the runner's user may only run
  `odt-deploy`, settings and secrets come from the GitHub environment, host settings from
  `/etc/odt/backend-host.env`.

## Consequences

- No build on the production host; the image CI checked is the image that runs. A
  rollback is a different `ODT_VERSION` (the host keeps the last 3 release images, the
  registry all of them).
- A release needs GitHub's API, raw files and the registry (anonymous API calls are limited
  to 60 an hour per address, a release uses three).
- Whoever can push to the package can change what a tag deploys: only this repository's
  workflows on `main` and tags have `packages: write`; pull requests do not.
- The image of every `main` push is public too (as is the code it is built from).
- The `image` job adds a few minutes before the deployment starts.
- Membership of the `docker` group is as good as root on the host, so the user `odt` mainly
  keeps deployments out of the admin's home (other projects' files, SSH keys, sudo), not out
  of the host.
- Moving production from the checkout to the deployment directory is a one-time step
  (docs/deployment.md, section 20).

## Alternatives considered

- **Keep building on the server** (ADR 0013) — no registry, but the build competes with OTP
  for memory and the clone stays.
- **A private package with a read token on the server** — a long-lived credential on the
  host for code that is public anyway.
- **A bare Git mirror on the server for the tag checks and files** — no API rate limit, but
  still a copy of the repository to keep, which this decision removes.
