"""
Celery tasks — pobieranie i odswiezanie cache feedow.

Architektura taskow:
─────────────────────────────────────────────────────────────
  STATYCZNE (download_time_1/2):
  ┌─ Beat co minute ──▶ refresh_static_feeds_task()
  │                        sprawdza download_time_1/2 == teraz
  │                        └─ .delay() ──▶ fetch_static_entry_task(id)
  └─────────────────────────────────────────────────────────

  REALTIME (interval per endpoint):
  ┌─ Beat raz przy starcie ──▶ bootstrap_realtime_tasks()
  │                              dla kazdego aktywnego endpointu
  │                              └─ fetch_realtime_endpoint_task.apply_async(
  │                                     countdown=stagger)  ← rozlozony start
  │
  │  fetch_realtime_endpoint_task(id)
  │    1. pobierz plik
  │    2. zapisz cached_file
  │    3. .apply_async(countdown=endpoint.interval)  ← zaplanuj siebie na pozniej
  └─────────────────────────────────────────────────────────

Zalety self-scheduling:
  • Brak pollingu DB co 10 s — Beat nie jest potrzebny dla RT po bootstrapie.
  • Kazdy endpoint respektuje swoj interval (np. 15 s, 30 s, 60 s).
  • Skalowalnosc — wiele endpointow moze pracowac niezaleznie.
"""

import random

from celery import shared_task
from celery.utils.log import get_task_logger
from django.core.cache import cache
from django.utils import timezone

logger = get_task_logger(__name__)

# Lekki lock per endpoint, aby unikac nakladania fetchy dla tego samego RT.
RT_ENDPOINT_LOCK_TTL_SECONDS = 300
# Ogranicza burst po starcie: endpointy sa uruchamiane z malym odstepem.
RT_BOOTSTRAP_STAGGER_SECONDS = 3
RT_BOOTSTRAP_JITTER_MAX_SECONDS = 2


def _scheduler():
    """Leniwy import — unika circular imports przy starcie Celery."""
    from data_manager.scheduler import (
        _fetch_realtime_endpoint,
        _fetch_static_entry,
        _completed_submission_ids,
    )
    return _fetch_static_entry, _fetch_realtime_endpoint, _completed_submission_ids


# ---------------------------------------------------------------------------
# STATYCZNE — dispatch co minutę przez Beat
# ---------------------------------------------------------------------------

@shared_task(name='data_manager.refresh_static_feeds')
def refresh_static_feeds_task() -> dict:
    """
    Uruchamiany przez Beat co minutę (crontab).
    Sprawdza download_time_1/2 == teraz i kolejkuje fetch per wpis.
    Lekki — tylko jedno zapytanie do DB + N .delay() calls.
    """
    from data_manager.models import StaticFeedEntry
    from django.db.models import Q
    _, _, _completed = _scheduler()

    now_time = timezone.now().time().replace(second=0, microsecond=0)
    completed_ids = _completed()

    entry_ids = list(
        StaticFeedEntry.objects.filter(
            hide_original=True,
            url__isnull=False,
            submission_id__in=completed_ids,
        ).filter(
            Q(download_time_1=now_time) | Q(download_time_2=now_time)
        ).values_list('id', flat=True)
    )

    for entry_id in entry_ids:
        fetch_static_entry_task.delay(entry_id)

    logger.info('Dispatched %d static feed tasks for %s', len(entry_ids), now_time)
    return {'dispatched': len(entry_ids), 'time': str(now_time)}


@shared_task(
    name='data_manager.fetch_static_entry',
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,   # 60 s -> 120 s -> 240 s
    retry_jitter=True,
)
def fetch_static_entry_task(entry_id: int) -> dict:
    """Pobiera i zapisuje cache dla jednego StaticFeedEntry."""
    from data_manager.models import StaticFeedEntry
    _fetch, _, _ = _scheduler()

    try:
        entry = StaticFeedEntry.objects.get(pk=entry_id)
    except StaticFeedEntry.DoesNotExist:
        logger.warning('StaticFeedEntry id=%d not found, skipping', entry_id)
        return {'status': 'not_found', 'entry_id': entry_id}

    _fetch(entry)
    return {'status': 'ok', 'entry_id': entry_id}


# ---------------------------------------------------------------------------
# REALTIME — self-scheduling, bez pollingu
# ---------------------------------------------------------------------------

@shared_task(name='data_manager.bootstrap_realtime_tasks')
def bootstrap_realtime_tasks() -> dict:
    """
    Uruchamiany JEDNORAZOWO przez Beat przy starcie systemu.
    Inicjuje petle self-scheduling dla kazdego aktywnego endpointu RT.
    Po tym Beat nie jest juz potrzebny dla taskow RT.

    Bezpieczny do wielokrotnego wywolania: uruchomienia sa rozkladane
    w czasie, ale deduplikacja kolejek nie opiera sie na task_id.
    """
    from data_manager.models import RealtimeEndpoint
    _, _, _completed = _scheduler()

    completed_ids = _completed()
    endpoint_ids = list(
        RealtimeEndpoint.objects.filter(
            hide_original=True,
            entry__submission_id__in=completed_ids,
        ).values_list('id', flat=True)
    )

    for index, endpoint_id in enumerate(endpoint_ids):
        # Rozloz start endpointow, aby uniknac jednoczesnego burstu.
        stagger = (index * RT_BOOTSTRAP_STAGGER_SECONDS) + random.randint(0, RT_BOOTSTRAP_JITTER_MAX_SECONDS)
        fetch_realtime_endpoint_task.apply_async(
            args=[endpoint_id],
            countdown=stagger,
        )

    logger.info('Bootstrapped %d realtime endpoint tasks', len(endpoint_ids))
    return {'bootstrapped': len(endpoint_ids)}


@shared_task(
    name='data_manager.fetch_realtime_endpoint',
    max_retries=2,
    # Nie uzywamy autoretry — przy bledzie chcemy nadal zaplanowac kolejne
    # wykonanie (zeby endpoint nie "wypadl" z petli po 2 bledach z rzedu).
)
def fetch_realtime_endpoint_task(endpoint_id: int) -> dict:
    """
    Pobiera cache dla jednego RealtimeEndpoint, a nastepnie planuje
    siebie na za `interval` sekund (self-scheduling loop).

    Bledy sieciowe sa zapisywane do FeedFetchError — petla kontynuuje,
    ale rownolegle wykonania tego samego endpointu sa blokowane lockiem.
    """
    from data_manager.models import RealtimeEndpoint
    _, _fetch, _completed = _scheduler()

    lock_key = f'rt-endpoint-lock:{endpoint_id}'
    # cache.add zwroci False, jesli lock juz istnieje.
    lock_acquired = cache.add(lock_key, '1', timeout=RT_ENDPOINT_LOCK_TTL_SECONDS)
    if not lock_acquired:
        logger.info('RealtimeEndpoint id=%d already running, skipping overlap', endpoint_id)
        return {'status': 'skipped_overlap', 'endpoint_id': endpoint_id}

    try:
        try:
            endpoint = RealtimeEndpoint.objects.select_related(
                'entry__submission'
            ).get(pk=endpoint_id)
        except RealtimeEndpoint.DoesNotExist:
            logger.info('RealtimeEndpoint id=%d deleted, stopping loop', endpoint_id)
            return {'status': 'stopped', 'endpoint_id': endpoint_id}

        # Sprawdz czy submission wciaz jest aktywne (stage 4, nie cofniete).
        completed_ids = set(_completed())
        if endpoint.entry.submission_id not in completed_ids:
            logger.info(
                'RealtimeEndpoint id=%d submission no longer completed, stopping loop',
                endpoint_id,
            )
            return {'status': 'stopped', 'endpoint_id': endpoint_id}

        now = timezone.now()
        try:
            _fetch(endpoint, now)
            status = 'ok'
        except Exception as exc:
            # Blad jest zapisywany do FeedFetchError przez _fetch.
            logger.warning('fetch_realtime_endpoint id=%d error: %s', endpoint_id, exc)
            status = 'error'
        finally:
            # Planuj kolejne wykonanie niezaleznie od sukcesu/bledu.
            fetch_realtime_endpoint_task.apply_async(
                args=[endpoint_id],
                countdown=endpoint.interval,
            )

        return {'status': status, 'endpoint_id': endpoint_id, 'next_in': endpoint.interval}
    finally:
        cache.delete(lock_key)
