#!/bin/sh
# Backs up what production cannot recreate: the database, uploaded_data/ (submitted feed
# files and their validation reports) and planner_data/snapshots/ (the last good version of
# each planner feed). odt-backup.timer runs it daily as the owner of the checkout
# (docs/deployment.md, section 16); it also works by hand from the checkout:
#
#   sh scripts/deploy/backup.sh
#
# The settings below come from the environment (odt-backup.service reads /etc/odt/backup.env)
# and are described in docs/deployment.md, section 16. A backup is written under a temporary
# name and renamed when complete, so a failed run leaves nothing that looks like a backup.
# Off the host it cannot be renamed, so its file `complete` is copied there last.
set -eu

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/scheduled}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
BACKUP_REMOTE_TOOL="${BACKUP_REMOTE_TOOL:-rsync}"
# Complete backups are named by their UTC start time.
NAME_PATTERN='????????T??????Z'

cd "$(dirname "$0")/../.."
log() { echo "backup: $*" >&2; }

case "$BACKUP_DIR" in
    /*) ;;
    *) log "BACKUP_DIR must be an absolute path, got '$BACKUP_DIR'"; exit 2 ;;
esac
case "$BACKUP_KEEP" in
    '' | *[!0-9]* | 0) log "BACKUP_KEEP must be a whole number from 1, got '$BACKUP_KEEP'"; exit 2 ;;
esac
case "$BACKUP_REMOTE_TOOL" in
    rsync | rclone) ;;
    *) log "BACKUP_REMOTE_TOOL must be rsync or rclone, got '$BACKUP_REMOTE_TOOL'"; exit 2 ;;
esac

name="$(date -u +%Y%m%dT%H%M%SZ)"
partial="$BACKUP_DIR/.partial-$name"
final="$BACKUP_DIR/$name"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
# One run at a time: a second one would delete the first one's partial backup below.
exec 9> "$BACKUP_DIR/.lock"
if ! flock -n 9; then
    log "another backup is running"
    exit 1
fi
# Leftovers of a run that was killed before its cleanup.
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -name '.partial-*' -exec rm -rf {} +
previous="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name "$NAME_PATTERN" \
    | sort | tail -n 1)"
on_exit() {
    status=$?
    rm -rf "$partial"
    [ "$status" -eq 0 ] || log "FAILED (exit $status)"
}
trap on_exit EXIT
mkdir -m 700 "$partial"

# Variables expanded inside the container, where the .env values are set.
./scripts/deploy.sh exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
    > "$partial/db.dump"
# pg_restore cannot list an empty or cut-short dump.
if ! ./scripts/deploy.sh exec -T postgres pg_restore --list < "$partial/db.dump" > /dev/null; then
    log "pg_restore cannot read the database dump"
    exit 1
fi

for data in uploaded_data planner_data/snapshots; do
    if [ ! -d "$data" ]; then
        # Without the planner there are no snapshots; uploaded_data/ always exists.
        [ "$data" = planner_data/snapshots ] || { log "no $data/ in $(pwd)"; exit 1; }
        log "no $data/ in $(pwd), skipped"
        continue
    fi
    mkdir -p "$partial/$data"
    # Files with the same content as in the previous backup become hard links to it. Compared
    # by checksum, not time: scheduled downloads rewrite unchanged feeds every day.
    # */dynamic/ holds the realtime proxy cache, rewritten every few seconds and refetched
    # from the source. Exit 24: a file vanished while it was copied (a validation finishing).
    rsync -a --no-times --checksum --exclude='/*/dynamic/' \
        ${previous:+"--link-dest=$previous/$data"} "$data/" "$partial/$data/" || [ $? -eq 24 ]
done

: > "$partial/complete"
mv "$partial" "$final"
log "$final complete (database $(du -h "$final/db.dump" | cut -f1))"
# Older local backups beyond the newest $BACKUP_KEEP.
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name "$NAME_PATTERN" | sort \
    | head -n "-$BACKUP_KEEP" | xargs -r rm -rf

# Only the new backup is copied: deleting old ones there is left to the destination, so a
# mistake or an intruder here cannot empty it.
if [ -z "$BACKUP_REMOTE" ]; then
    log "WARNING: BACKUP_REMOTE is not set, so this backup exists on this disk only"
elif [ "$BACKUP_REMOTE_TOOL" = rclone ]; then
    rclone copy --exclude /complete "$final" "$BACKUP_REMOTE/$name"
    rclone copy "$final/complete" "$BACKUP_REMOTE/$name"
else
    # Files linked locally are linked to the previous backup there too, if it has it.
    rsync -a --exclude=/complete ${previous:+"--link-dest=../${previous##*/}"} \
        "$final/" "$BACKUP_REMOTE/$name/"
    rsync -a "$final/complete" "$BACKUP_REMOTE/$name/"
fi
[ -z "$BACKUP_REMOTE" ] || log "copied to $BACKUP_REMOTE/$name"
