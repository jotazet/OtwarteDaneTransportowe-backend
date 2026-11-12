# 0014. Back up daily to hard-linked local snapshots and copy each one off the host

- **Status:** Proposed
- **Date:** 2026-11-12

## Context

Production runs on one VPS. Until now the only automated database dump was taken at release
time, into `~/backups` on the same disk, and the documented manual dump wrote empty files
(issues #67, #68). `uploaded_data/` (submitted feed files and their history) and
`planner_data/snapshots/` (the last good version of each planner feed) were never backed up,
and neither can be recreated. A disk loss, or a long gap between releases, would lose
submissions and feeds that organizers sent once.

## Decision

- `scripts/deploy/backup.sh`, run daily by a systemd timer (`odt-backup.timer`) as the owner of
  the checkout, dumps the database (`pg_dump -Fc`, variables expanded in the container,
  checked with `pg_restore --list`) and copies `uploaded_data/` (without the realtime cache)
  and `planner_data/snapshots/` into `~/backups/scheduled/<UTC time>/`. Files unchanged since
  the previous backup are hard links to it (`rsync --link-dest`); the newest 7 are kept.
- Each new backup is copied off the host with rsync or rclone to `BACKUP_REMOTE`, set in the
  root-owned `/etc/odt/backup.env`. The destination is the team's choice and is not in the
  repository. The script never deletes there: retention belongs to the destination.
- Any failure (dump, its check, the copy, the upload) exits non-zero and shows in the journal
  and `systemctl --failed`. An incomplete backup never gets a backup's name.

## Consequences

- Restore and restore tests are documented in `docs/deployment.md`, section 16.
- Until `BACKUP_REMOTE` is set, backups are on the server's disk only (the journal warns daily).
- Nothing alerts on a failed run yet; someone has to look, or an `OnFailure=` notifier is added.
- `.env` is not in the backups: the secrets, above all `FEED_AUTH_ENCRYPTION_KEY`, must be kept
  in a password vault, because GitHub cannot show a secret again.
- The unit files hold the production user and path; they are reinstalled by hand when they
  change, while the script itself changes with each release.

## Alternatives considered

- **Mirror the whole backup directory** (`rsync --delete`, `rclone sync`) — one command and
  retention in one place, but a mistake or an intruder on the server would empty the copy too.
- **Daily tar archives** — simple, but each day costs the full size of the feed files.
- **A cron job** — no `Persistent=` catch-up after downtime and failures only reach local mail.
- **Volume or VPS snapshots from the hosting provider** — not verified for this VPS, stay with
  the same provider, and the database inside one is only crash-consistent.
