#!/bin/sh
# Deploys the checked-out release, in the order that keeps the data safe: the .env check,
# a database dump, the new .env, the image, migrations and new containers, then an answer
# from the API. Any failure stops it there. odt-deploy runs it after checking out the tag
# (docs/deployment.md, section 20); it also works by hand from the checkout:
#
#   sh scripts/deploy/release.sh <label> <new .env>
set -eu

tag="$1"
env_new="$2"
BACKUPS_KEEP=10
HEALTH_TRIES=36
HEALTH_DELAY=5

cd "$(dirname "$0")/../.."
log() { echo "release: $*"; }

python3 -I scripts/deploy/envfile.py check .env "$env_new"

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

# The running image stays as :previous for a quick manual rollback (section 20).
docker image tag odt-backend:latest odt-backend:previous 2>/dev/null || true
# --pull: the newest python:3.12-slim of the same tag, so base image fixes reach production.
./scripts/deploy.sh build --pull web
# `migrate` runs first (depends_on), then every service is recreated on the new image.
./scripts/deploy.sh up -d

tries=0
until ./scripts/deploy.sh exec -T web python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/admin/login/', timeout=5)" \
    > /dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge "$HEALTH_TRIES" ]; then
        log "web did not answer after $tries tries" >&2
        ./scripts/deploy.sh ps >&2
        exit 1
    fi
    sleep "$HEALTH_DELAY"
done
log "$tag is live"

docker image prune -f > /dev/null
