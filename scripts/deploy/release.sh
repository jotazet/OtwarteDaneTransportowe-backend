#!/bin/sh
# Deploys the checked-out release, in the order that keeps the data safe: the .env check,
# the image (which changes nothing running), a database dump, the new .env, migrations and
# new containers, then every service healthy. Any failure stops it there. odt-deploy runs
# it after checking out the tag (docs/deployment.md, section 20); it also works by hand from
# the checkout, with a complete .env (odt-deploy adds the host file's settings to the job's):
#
#   sh scripts/deploy/release.sh vX.Y.Z <new .env>
set -eu

tag="$1"
env_new="$2"
BACKUPS_KEEP=10
RELEASES_KEEP=3
# Covers the migrations too: when it expires, Compose leaves the services after `migrate`
# created but not started. Within the deploy job's 30 minutes.
WAIT_TIMEOUT=900

cd "$(dirname "$0")/../.."
log() { echo "release: $*"; }

# The version becomes an image tag (odt-backend:vX.Y.Z) once the release is live.
if ! printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+'; then
    log "expected a release tag vX.Y.Z, got '$tag'" >&2
    exit 1
fi

# A planner graph build or feed test (containers labelled odt.planner) holds up to 8 GB of
# RAM and swap: an image build beside it risks the OOM killer, and the release would
# restart the worker that runs it. Nothing has changed yet, so release again later.
if [ -n "$(docker ps -q --filter label=odt.planner)" ]; then
    log "a planner graph build is running; release again when it has finished" >&2
    exit 1
fi

python3 -I scripts/deploy/envfile.py check .env "$env_new"

# --pull: the newest python:3.12-slim of the same tag, so base image fixes reach production.
./scripts/deploy.sh build --pull web

# The dump, and the .env and commit it belongs to, before anything changes.
backup="$HOME/backups/release-$(date -u +%Y%m%dT%H%M%SZ)-$tag"
mkdir -p "$HOME/backups"
mkdir -m 700 "$backup"
./scripts/deploy.sh exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
    > "$backup/db.dump"
[ ! -f .env ] || cp -p .env "$backup/env"
git rev-parse HEAD > "$backup/commit"
log "backup in $backup ($(du -h "$backup/db.dump" | cut -f1))"
# Older release backups beyond the newest $BACKUPS_KEEP.
find "$HOME/backups" -maxdepth 1 -name 'release-*' -type d | sort | head -n "-$BACKUPS_KEEP" \
    | xargs -r rm -rf

install -m 600 "$env_new" .env
# `migrate` runs first (depends_on), then every service is recreated on the new image.
# --wait: until every service runs and passes its healthcheck; it fails as soon as one
# exits or turns unhealthy, and one that keeps restarting never becomes healthy.
if ! ./scripts/deploy.sh up -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
    log "not every service became healthy within ${WAIT_TIMEOUT}s" >&2
    ./scripts/deploy.sh ps -a >&2
    exit 1
fi
# Only a release that passed gets an image tag, so a failed one, retried or not, never
# replaces a good rollback image (section 20). The newest $RELEASES_KEEP stay.
docker image tag odt-backend:latest "odt-backend:$tag"
log "$tag is live"

docker image ls odt-backend --format '{{.Tag}}' | grep -Ex 'v[0-9]+\.[0-9]+\.[0-9]+' \
    | sort -V | head -n "-$RELEASES_KEEP" | sed 's/^/odt-backend:/' \
    | xargs -r docker image rm > /dev/null || log "could not remove old release images"
# Only this project's replaced images: the host runs other projects too.
docker image prune -f --filter label=org.opencontainers.image.title=odt-backend > /dev/null
# Build cache older than a week, of every project on the host (it grew to 17 GB): only a
# cache, so another project at most builds slower. The release is live either way.
docker builder prune -f --filter until=168h > /dev/null || log "could not prune the build cache"
