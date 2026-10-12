"""
Re-queue verification for realtime submissions stuck below stage 3.

``tasks.enqueue`` deliberately swallows broker failures so a Redis outage cannot
turn a successful write into an HTTP 500. Every other queued job had a way back:
static validations via ``retry_gtfs_validation``, realtime fetch loops via the
periodic ``bootstrap_realtime_tasks``. A lost ``validate_realtime_submission_task``
had none — the submission simply sat at stage 1/2 forever with nothing to retry
it. This command closes that gap.

Usage:
    python manage.py retry_realtime_validation
    python manage.py retry_realtime_validation --submission-id 12
    python manage.py retry_realtime_validation --older-than-minutes 30
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from data_manager.models import RealtimeSubmission
from data_manager.tasks import enqueue, validate_realtime_submission_task


class Command(BaseCommand):
    help = 'Re-queue validate_realtime_submission_task for submissions stuck below stage 3.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--submission-id',
            type=int,
            action='append',
            dest='submission_ids',
            help='Only retry these realtime submission IDs (repeatable).',
        )
        parser.add_argument(
            '--older-than-minutes',
            type=int,
            default=15,
            help=(
                'Skip submissions updated more recently than this, so a verification '
                'still in flight is not queued twice. Default: 15.'
            ),
        )

    def handle(self, *args, **options):
        submissions = RealtimeSubmission.objects.prefetch_related('history', 'endpoints')

        submission_ids = options.get('submission_ids')
        if submission_ids:
            submissions = submissions.filter(pk__in=submission_ids)
        else:
            cutoff = timezone.now() - timedelta(minutes=options['older_than_minutes'])
            submissions = submissions.filter(updated_at__lte=cutoff)

        queued = 0
        for rts in submissions:
            if rts.current_stage >= 3:
                continue
            if rts.is_rejected:
                # Rejected submissions wait for the owner to resubmit; requeuing
                # would re-reject them against the same broken endpoints.
                continue
            if not rts.endpoints.all():
                self.stderr.write(f'Skip realtime submission {rts.id}: no endpoints defined.')
                continue
            # enqueue(), not .delay(): this command exists precisely for the
            # case where the broker was unreachable, so it must report a still
            # unreachable broker rather than traceback halfway through the list.
            if enqueue(validate_realtime_submission_task, rts.id):
                queued += 1
                self.stdout.write(
                    f'Queued verification for RealtimeSubmission {rts.id} (stage {rts.current_stage}).'
                )
            else:
                self.stderr.write(
                    f'Could not queue RealtimeSubmission {rts.id} — broker unreachable.'
                )

        self.stdout.write(self.style.SUCCESS(f'Queued {queued} realtime verification task(s).'))
