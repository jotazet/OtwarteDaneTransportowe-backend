"""Retention and statistics of the planner request log.

Raw rows (with IP addresses) are kept PLANNER_REQUEST_LOG_RETENTION_DAYS; every
finished day is first summarised into PlannerDailyStat, which holds no IPs.
"""

from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from planner.models import PlannerDailyStat, PlannerRequestLog

PURGE_BATCH = 10_000
TOP_CLIENTS = 20


def roll_up_and_purge(today: date | None = None) -> None:
    """Summarise every finished day not summarised yet, then drop expired rows. Idempotent."""
    today = today or timezone.now().date()
    summarised = set(PlannerDailyStat.objects.values_list('date', flat=True).distinct())
    for day in PlannerRequestLog.objects.filter(created_at__date__lt=today).dates(
        'created_at', 'day'
    ):
        if day in summarised:
            continue
        rows = (
            PlannerRequestLog.objects.filter(created_at__date=day)
            .values('endpoint', 'api_key', 'status_code')
            .annotate(
                requests=Count('id'),
                latency_ms_sum=Sum('latency_ms'),
                latency_ms_max=Max('latency_ms'),
            )
        )
        with transaction.atomic():
            PlannerDailyStat.objects.bulk_create(
                PlannerDailyStat(
                    date=day,
                    endpoint=row['endpoint'],
                    api_key_id=row['api_key'],
                    status_code=row['status_code'],
                    requests=row['requests'],
                    latency_ms_sum=row['latency_ms_sum'],
                    latency_ms_max=row['latency_ms_max'],
                )
                for row in rows
            )
    cutoff = timezone.now() - timedelta(days=settings.PLANNER_REQUEST_LOG_RETENTION_DAYS)
    while True:
        ids = list(
            PlannerRequestLog.objects.filter(created_at__lt=cutoff).values_list('id', flat=True)[
                :PURGE_BATCH
            ]
        )
        if not ids:
            return
        PlannerRequestLog.objects.filter(id__in=ids).delete()


def daily_stats(date_from: date, date_to: date, endpoint=None, api_key=None) -> list[dict]:
    """Requests per day by status class; today comes live from the raw log."""
    filters = {}
    if endpoint:
        filters['endpoint'] = endpoint
    if api_key:
        filters['api_key'] = api_key
    rows = list(
        PlannerDailyStat.objects.filter(date__range=(date_from, date_to), **filters).values(
            'date', 'status_code', 'requests', 'latency_ms_sum', 'latency_ms_max'
        )
    )
    today = timezone.now().date()
    if date_from <= today <= date_to:
        rows += [
            {**row, 'date': today}
            for row in PlannerRequestLog.objects.filter(created_at__date=today, **filters)
            .values('status_code')
            .annotate(
                requests=Count('id'),
                latency_ms_sum=Sum('latency_ms'),
                latency_ms_max=Max('latency_ms'),
            )
        ]
    days = defaultdict(
        lambda: {
            'requests': 0,
            'ok': 0,
            'client_errors': 0,
            'rate_limited': 0,
            'server_errors': 0,
            'latency_ms_sum': 0,
            'max_latency_ms': 0,
        }
    )
    for row in rows:
        day = days[row['date']]
        day['requests'] += row['requests']
        day[_status_class(row['status_code'])] += row['requests']
        day['latency_ms_sum'] += row['latency_ms_sum']
        day['max_latency_ms'] = max(day['max_latency_ms'], row['latency_ms_max'])
    return [
        {
            'date': day,
            **{k: v for k, v in totals.items() if k != 'latency_ms_sum'},
            'avg_latency_ms': round(totals['latency_ms_sum'] / totals['requests']),
        }
        for day, totals in sorted(days.items())
    ]


def top_clients(days: int, endpoint=None) -> list[dict]:
    """Clients with the most requests in the last ``days`` days (raw log only)."""
    rows = PlannerRequestLog.objects.filter(created_at__gte=timezone.now() - timedelta(days=days))
    if endpoint:
        rows = rows.filter(endpoint=endpoint)
    return list(
        rows.values('client_ip', 'api_key', 'api_key__name', 'api_key__prefix')
        .annotate(requests=Count('id'))
        .order_by('-requests')[:TOP_CLIENTS]
    )


def _status_class(code: int) -> str:
    if code == 429:
        return 'rate_limited'
    if code >= 500:
        return 'server_errors'
    if code >= 400:
        return 'client_errors'
    return 'ok'
