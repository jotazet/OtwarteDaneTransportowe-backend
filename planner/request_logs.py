"""Retention and statistics of the planner request log (admin dashboard).

Raw rows (with IP addresses) are kept PLANNER_REQUEST_LOG_RETENTION_DAYS; every
finished day is first summarised into PlannerDailyStat, which holds no IPs.
Everything per IP, per minute or per hour comes from the raw rows.
"""

import math
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Aggregate, Avg, Count, FloatField, IntegerField, Max, Min, Q, Sum
from django.db.models.functions import Cast, Collate, Trunc
from django.utils import timezone

from planner.models import PlannerDailyStat, PlannerRequestLog

PURGE_BATCH = 10_000
INTERVALS = {'minute': timedelta(minutes=1), 'hour': timedelta(hours=1), 'day': timedelta(days=1)}
# A chart has at most this many points: 16 hours by the minute, 41 days by the hour.
MAX_BUCKETS = 1000
# A chart split by endpoint shows the busiest ones and puts the rest in "other".
SPLIT_ENDPOINTS = 8
# The one definition of the status classes (429 is rate_limited, not a client error).
STATUS_CLASSES = {
    'ok': Q(status_code__lt=400),
    'client_errors': Q(status_code__gte=400, status_code__lt=500) & ~Q(status_code=429),
    'rate_limited': Q(status_code=429),
    'server_errors': Q(status_code__gte=500),
}


class Percentile(Aggregate):
    """PostgreSQL's PERCENTILE_CONT(fraction) WITHIN GROUP (ORDER BY expression)."""

    function = 'PERCENTILE_CONT'
    template = '%(function)s(%(fraction)s) WITHIN GROUP (ORDER BY %(expressions)s)'
    output_field = FloatField()

    def __init__(self, expression, fraction: float, **extra):
        super().__init__(expression, fraction=float(fraction), **extra)


class Mode(Aggregate):
    """PostgreSQL's MODE() WITHIN GROUP (ORDER BY expression): the most frequent value."""

    function = 'MODE'
    template = '%(function)s() WITHIN GROUP (ORDER BY %(expressions)s)'


def _class_counts(count=lambda q: Count('id', filter=q)) -> dict:
    return {name: count(q) for name, q in STATUS_CLASSES.items()}


def _latency() -> dict:
    # Whole milliseconds, rounded by PostgreSQL's cast.
    return {
        'avg_latency_ms': Cast(Avg('latency_ms'), IntegerField()),
        'p95_latency_ms': Cast(Percentile('latency_ms', 0.95), IntegerField()),
        'max_latency_ms': Max('latency_ms'),
    }


# Sorting text in the database's locale made grouping by user agent several times
# slower (16 s vs 3 s at 3M rows); byte order is all a group or a mode needs.
def _byte_order(field: str):
    return Collate(field, 'C')


def filtered(since, until, endpoint=None, api_key=None, ip=None, status=None):
    """Raw log rows in [since, until) matching the dashboard and request-log filters."""
    rows = PlannerRequestLog.objects.filter(created_at__gte=since, created_at__lt=until)
    if endpoint:
        rows = rows.filter(endpoint=endpoint)
    if api_key is not None:
        rows = rows.filter(api_key_id=api_key)
    if ip:
        rows = rows.filter(client_ip=ip)
    if status is not None:
        rows = rows.filter(status_code=status)
    return rows


def share(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def summary(rows) -> dict:
    return rows.aggregate(
        requests=Count('id'),
        unique_ips=Count('client_ip', distinct=True),
        unique_api_keys=Count('api_key', distinct=True),
        **_class_counts(),
        **_latency(),
    )


def bucket_count(since: datetime, until: datetime, interval: str) -> int:
    """How many buckets cover [since, until), without building them."""
    return math.ceil((until - _first_bucket(since, interval)) / INTERVALS[interval])


def bucket_starts(since: datetime, until: datetime, interval: str) -> list[datetime]:
    """UTC bucket starts covering [since, until); check bucket_count first."""
    first = _first_bucket(since, interval)
    return [first + n * INTERVALS[interval] for n in range(bucket_count(since, until, interval))]


def _first_bucket(since: datetime, interval: str) -> datetime:
    # Every interval divides a UTC day, so flooring the timestamp aligns buckets.
    seconds = INTERVALS[interval].total_seconds()
    return datetime.fromtimestamp(since.timestamp() // seconds * seconds, UTC)


def timeseries(rows, since: datetime, until: datetime, interval: str, split: str) -> dict:
    """Requests per bucket for a chart, every bucket present (zero when empty), in UTC.

    ``split``: ``status`` (one series per status class), ``endpoint`` (the
    busiest endpoints and ``other``) or ``none`` (one series ``all``).
    """
    buckets = bucket_starts(since, until, interval)
    in_buckets = rows.annotate(bucket=Trunc('created_at', interval, tzinfo=UTC))
    per_bucket = {
        row['bucket']: row
        for row in in_buckets.values('bucket').annotate(
            all=Count('id'),
            **_class_counts(),
            avg=Cast(Avg('latency_ms'), IntegerField()),
            p95=Cast(Percentile('latency_ms', 0.95), IntegerField()),
        )
    }

    if split == 'endpoint':
        counts, totals = {}, {}
        for row in in_buckets.values('bucket', 'endpoint').annotate(n=Count('id')):
            counts[row['endpoint'], row['bucket']] = row['n']
            totals[row['endpoint']] = totals.get(row['endpoint'], 0) + row['n']
        busiest = sorted(totals, key=lambda name: (-totals[name], name))[:SPLIT_ENDPOINTS]

        def requests(name, bucket):
            if name != 'other':
                return counts.get((name, bucket), 0)
            return (per_bucket.get(bucket) or {}).get('all', 0) - sum(
                counts.get((busy, bucket), 0) for busy in busiest
            )

        names = busiest + (['other'] if len(totals) > len(busiest) else [])
    else:
        names = list(STATUS_CLASSES) if split == 'status' else ['all']

        def requests(name, bucket):
            return (per_bucket.get(bucket) or {}).get(name, 0)

    return {
        'interval': interval,
        'split': split,
        'buckets': buckets,
        'series': [
            {'name': name, 'requests': [requests(name, bucket) for bucket in buckets]}
            for name in names
        ],
        'latency_ms': {
            kind: [(per_bucket.get(bucket) or {}).get(kind) for bucket in buckets]
            for kind in ('avg', 'p95')
        },
    }


def endpoints(rows) -> list[dict]:
    """Requests, errors and latency per endpoint, busiest first."""
    table = list(
        rows.values('endpoint')
        .annotate(requests=Count('id'), **_class_counts(), **_latency())
        .order_by('-requests', 'endpoint')
    )
    total = sum(row['requests'] for row in table)  # every request has one endpoint
    return [{**row, 'share': share(row['requests'], total)} for row in table]


def top_clients(rows, by: str = 'ip'):
    """Busiest clients by IP address or by API key, as a queryset of dicts (paginate it)."""
    if by == 'api_key':
        keys = ['api_key', 'api_key__name', 'api_key__prefix']
        grouped = rows.filter(api_key__isnull=False).values(*keys)
        extra = {'ips': Count('client_ip', distinct=True)}
    else:
        keys = ['client_ip']
        grouped = rows.values(*keys)
        extra = {'api_keys': Count('api_key', distinct=True)}
    return grouped.annotate(
        requests=Count('id'),
        **_class_counts(),
        endpoints=Count('endpoint', distinct=True),
        first_seen=Min('created_at'),
        last_seen=Max('created_at'),
        user_agent=Mode(_byte_order('user_agent')),
        **extra,
    ).order_by('-requests', keys[0])


def user_agents(rows):
    """User-Agents by requests, with how many IPs sent each (a queryset of dicts)."""
    return (
        rows.values(agent=_byte_order('user_agent'))
        .annotate(requests=Count('id'), ips=Count('client_ip', distinct=True))
        .order_by('-requests', 'agent')
    )


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
    """Requests per day by status class, from the summaries and, for days not yet summarised, the log."""
    filters = {}
    if endpoint:
        filters['endpoint'] = endpoint
    if api_key:
        filters['api_key'] = api_key
    rows = list(
        PlannerDailyStat.objects.filter(date__range=(date_from, date_to), **filters)
        .values('date')
        # Not `requests=`: that name would hide the field the class sums read.
        .annotate(
            total=Sum('requests'),
            **_class_counts(lambda q: Sum('requests', filter=q, default=0)),
            latency_ms_sum=Sum('latency_ms_sum'),
            max_latency_ms=Max('latency_ms_max'),
        )
    )
    for row in rows:
        row['requests'] = row.pop('total')
    # Days not summarised yet (today, and yesterday until the nightly rollup)
    # come from the raw log.
    summarised = set(PlannerDailyStat.objects.values_list('date', flat=True).distinct())
    rows += [
        {**row, 'date': row.pop('created_at__date')}
        for row in PlannerRequestLog.objects.filter(
            created_at__date__range=(date_from, date_to), **filters
        )
        .values('created_at__date')
        .annotate(
            requests=Count('id'),
            **_class_counts(),
            latency_ms_sum=Sum('latency_ms'),
            max_latency_ms=Max('latency_ms'),
        )
        if row['created_at__date'] not in summarised
    ]
    return [
        {
            **{key: value for key, value in row.items() if key != 'latency_ms_sum'},
            'avg_latency_ms': round(row['latency_ms_sum'] / row['requests']),
        }
        for row in sorted(rows, key=lambda row: row['date'])
    ]
