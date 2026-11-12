#!/bin/sh
# Deploys a release from the deployment directory, in the order that keeps the data safe:
# the .env check, the release's image (which changes nothing running), a database dump,
# the new .env, migrations and new containers, then every service healthy. Any failure
# stops it there. odt-deploy runs it after copying the release's Compose files and scripts
# into the deployment directory (docs/deployment.md, section 20); it also works by hand from
# there, with a complete .env (odt-deploy adds the host file's settings to the job's):
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

# The version is the tag of the image CI pushed for it (ghcr.io/...:vX.Y.Z).
if ! printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+'; then
    log "expected a release tag vX.Y.Z, got '$tag'" >&2
    exit 1
fi

# A planner graph build or feed test (containers labelled odt.planner) holds up to 8 GB of
# RAM and swap, and the release would restart the worker that runs it. Nothing has changed
# yet, so release again later.
if [ -n "$(docker ps -q --filter label=odt.planner)" ]; then
    log "a planner graph build is running; release again when it has finished" >&2
    exit 1
fi

python3 -I scripts/deploy/envfile.py check .env "$env_new"

# Compose takes the image tag from ODT_VERSION (docker-compose.prod.yml).
printf 'ODT_VERSION=%s\n' "$tag" >> "$env_new"
./scripts/deploy.sh --env-file "$env_new" pull --quiet web
# Lists the images of web's dependencies too; the application image is the one of the tag.
image="$(./scripts/deploy.sh --env-file "$env_new" config --images web | grep -m 1 ":$tag\$")"

# The dump, and the .env and version it belongs to, before anything changes.
backup="$HOME/backups/release-$(date -u +%Y%m%dT%H%M%SZ)-$tag"
mkdir -p "$HOME/backups"
mkdir -m 700 "$backup"
./scripts/deploy.sh exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
    > "$backup/db.dump"
[ ! -f .env ] || cp -p .env "$backup/env"
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
log "$tag is live"

# The images of the newest $RELEASES_KEEP releases stay for a rollback (section 20).
repository="${image%:*}"
docker image ls "$repository" --format '{{.Tag}}' | grep -Ex 'v[0-9]+\.[0-9]+\.[0-9]+' \
    | sort -V | head -n "-$RELEASES_KEEP" | sed "s|^|$repository:|" \
    | xargs -r docker image rm > /dev/null || log "could not remove old release images"
