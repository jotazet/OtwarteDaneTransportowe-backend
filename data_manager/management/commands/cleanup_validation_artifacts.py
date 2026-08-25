"""
Reclaim leftover GTFS-validator work directories under MEDIA_ROOT.

``validate_gtfs_feed_task`` now removes both work dirs in a ``finally``, but a
worker killed outright (hard ``time_limit``, OOM, SIGKILL, container restart)
still cannot run it, and every run before that fix leaked its directory on any
unexpected exit. The names carry a random suffix, so a leaked dir is never
reused and they accumulate.

Usage:
    python manage.py cleanup_validation_artifacts            # report only
    python manage.py cleanup_validation_artifacts --delete
    python manage.py cleanup_validation_artifacts --delete --min-age-hours 24
"""
import os
import shutil
import time

from django.conf import settings
from django.core.management.base import BaseCommand

WORKDIR_PREFIXES = ('validation_report_', 'tmp_')


class Command(BaseCommand):
    help = 'Find (and optionally delete) orphaned GTFS-validator work directories.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Actually remove the directories (default is a dry run).',
        )
        parser.add_argument(
            '--min-age-hours',
            type=float,
            default=6.0,
            help=(
                'Only consider directories older than this, so a validation '
                'running right now is never deleted underneath it. Default: 6.'
            ),
        )

    def handle(self, *args, **options):
        media_root = str(settings.MEDIA_ROOT)
        cutoff = time.time() - options['min_age_hours'] * 3600

        candidates = []
        for dirpath, dirnames, _filenames in os.walk(media_root):
            for name in list(dirnames):
                if not name.startswith(WORKDIR_PREFIXES):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                if mtime > cutoff:
                    continue
                candidates.append(full)
                # Do not descend into a directory already marked for removal.
                dirnames.remove(name)

        if not candidates:
            self.stdout.write(self.style.SUCCESS('No orphaned validator work directories found.'))
            return

        for path in sorted(candidates):
            self.stdout.write(f'  {os.path.relpath(path, media_root)}')

        if not options['delete']:
            self.stdout.write('')
            self.stdout.write(
                f'{len(candidates)} orphaned director(ies). Re-run with --delete to remove them.'
            )
            return

        removed = 0
        for path in candidates:
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                removed += 1
            else:
                self.stderr.write(f'Could not remove {path}')
        self.stdout.write(self.style.SUCCESS(f'Removed {removed} orphaned director(ies).'))
