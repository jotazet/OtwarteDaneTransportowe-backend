"""
Re-queue GTFS validation for submissions stuck at stage 2 (pending / failed Docker).

Usage:
    python manage.py retry_gtfs_validation
    python manage.py retry_gtfs_validation --submission-id 64
"""
from django.core.management.base import BaseCommand

from data_manager.models import FeedSubmission, StaticFeedEntry
from data_manager.tasks import validate_gtfs_feed_task


class Command(BaseCommand):
    help = 'Re-queue validate_gtfs_feed_task for entries stuck without completing validation.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--submission-id',
            type=int,
            action='append',
            dest='submission_ids',
            help='Only retry these feed submission IDs (repeatable).',
        )

    def handle(self, *args, **options):
        submission_ids = options.get('submission_ids')
        entries = StaticFeedEntry.objects.filter(
            submission__data_type='gtfs',
        ).select_related('submission')

        if submission_ids:
            entries = entries.filter(submission_id__in=submission_ids)

        queued = 0
        for entry in entries:
            sub = entry.submission
            if sub.current_stage < 2:
                continue
            if sub.current_stage >= 4:
                continue
            if not (entry.file or entry.cached_file):
                self.stderr.write(
                    f'Skip entry {entry.id} (submission {sub.id}): no file to validate.'
                )
                continue
            validate_gtfs_feed_task.delay(entry.id)
            queued += 1
            self.stdout.write(
                f'Queued validation for StaticFeedEntry {entry.id} '
                f'(submission {sub.id}, stage {sub.current_stage}).'
            )

        self.stdout.write(self.style.SUCCESS(f'Queued {queued} validation task(s).'))
