#!/bin/sh
# Backs up what production cannot recreate: the database, uploaded_data/ (submitted feed
# files and their validation reports) and planner_data/snapshots/ (the last good version of
# each planner feed). odt-backup.timer runs it daily as the owner of the checkout
# (docs/deployment.md, section 16); it also works by hand from the checkout:
#
#   sh scripts/deploy/backup.sh
#
# Settings come from the environment; odt-backup.service reads them from /etc/odt/backup.env:
#   BACKUP_DIR          absolute path of the local backups (default ~/backups/scheduled)
#   BACKUP_KEEP         how many local backups to keep (default 7)
#   BACKUP_REMOTE       where each new backup is copied off the host: an rsync destination
#                       (user@host:path, or a mounted path) or an rclone remote:path.
#                       Unset: the backup stays on this disk, with a warning.
#   BACKUP_REMOTE_TOOL  rsync (default) or rclone
#
# A backup is written under a temporary name and renamed when complete, so a failed run
# leaves nothing that looks like a backup. Files unchanged since the previous backup are hard
# links to it, so a day costs the database dump plus the files that changed.
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
if [ ! -s "$partial/db.dump" ] \
    || ! ./scripts/deploy.sh exec -T postgres pg_restore --list < "$partial/db.dump" > /dev/null; then
    log "the database dump is empty or pg_restore cannot read it"
    exit 1
fi

for data in uploaded_data planner_data/snapshots; do
    if [ ! -d "$data" ]; then
        log "no $data/ in $(pwd), skipped"
        continue
    fi
    mkdir -p "$partial/$data"
    # */dynamic/ holds the realtime proxy cache, rewritten every few seconds and refetched
    # from the source. Exit 24: a file vanished while it was copied (a validation finishing).
    rsync -a --exclude='/*/dynamic/' ${previous:+"--link-dest=$previous/$data"} \
        "$data/" "$partial/$data/" || [ $? -eq 24 ]
done

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
    rclone copy "$final" "$BACKUP_REMOTE/$name"
    log "copied to $BACKUP_REMOTE/$name"
else
    # Unchanged files become hard links to the previous backup there, if it has it.
    rsync -a ${previous:+"--link-dest=../${previous##*/}"} "$final/" "$BACKUP_REMOTE/$name/"
    log "copied to $BACKUP_REMOTE/$name"
fi
