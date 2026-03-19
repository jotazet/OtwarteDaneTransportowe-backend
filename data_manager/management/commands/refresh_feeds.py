"""
Management command: refresh_feeds

Narzędzie do ręcznego/debugowego odświeżenia cache feedów statycznych.
Przydatne podczas developmentu lub gdy Celery nie jest uruchomione.

Uwaga: feedy realtime są obsługiwane wyłącznie przez Celery (self-scheduling).
       Aby wystartować pętlę RT: python manage.py shell -c
       "from data_manager.tasks import bootstrap_realtime_tasks; bootstrap_realtime_tasks.delay()"

Użycie:
    python manage.py refresh_feeds   # odświeża feedy statyczne zaplanowane na teraz
"""
from django.core.management.base import BaseCommand

from data_manager.scheduler import _fetch_static_entry, _completed_submission_ids
from data_manager.models import StaticFeedEntry
from django.db.models import Q
from django.utils import timezone


class Command(BaseCommand):
    help = 'Ręcznie odświeża cache feedów statycznych (narzędzie debugowe).'

    def handle(self, *args, **options):
        now_time = timezone.now().time().replace(second=0, microsecond=0)
        completed_ids = _completed_submission_ids()

        entries = StaticFeedEntry.objects.filter(
            hide_original=True,
            url__isnull=False,
            submission_id__in=completed_ids,
        ).filter(
            Q(download_time_1=now_time) | Q(download_time_2=now_time)
        )

        count = 0
        for entry in entries:
            self.stdout.write(f'  → Pobieranie entry id={entry.pk}  {entry.url}')
            _fetch_static_entry(entry)
            count += 1

        self.stdout.write(self.style.SUCCESS(f'✓ Odświeżono {count} feed(ów) statycznych.'))
