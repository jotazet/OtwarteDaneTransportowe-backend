"""Retention and statistics of the planner request log (admin dashboard).

Raw rows (with IP addresses) are kept PLANNER_REQUEST_LOG_RETENTION_DAYS; every
finished day is first summarised into PlannerDailyStat, which holds no IPs.
Everything per IP, per minute or per hour comes from the raw rows.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import (
    Aggregate,
    Avg,
    Case,
    CharField,
    Count,
    F,
    FloatField,
    Max,
    Min,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Trunc
from django.utils import timezone

from planner.models import PlannerDailyStat, PlannerRequestLog

PURGE_BATCH = 10_000
INTERVALS = {'minute': timedelta(minutes=1), 'hour': timedelta(hours=1), 'day': timedelta(days=1)}
# A chart has at most this many points: 16 hours by the minute, 41 days by the hour.
MAX_BUCKETS = 1000
# A chart split by endpoint shows the busiest ones and puts the rest in "other".
SPLIT_ENDPOINTS = 8
STATUS_CLASSES = ('ok', 'client_errors', 'rate_limited', 'server_errors')


class TooManyBuckets(Exception):
    pass


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
    output_field = CharField()


def _class_counts() -> dict:
    """Requests per status class, as in daily_stats (429 is rate_limited, not a client error)."""
    return {
        'ok': Count('id', filter=Q(status_code__lt=400)),
        'client_errors': Count(
            'id', filter=Q(status_code__gte=400, status_code__lt=500) & ~Q(status_code=429)
        ),
        'rate_limited': Count('id', filter=Q(status_code=429)),
        'server_errors': Count('id', filter=Q(status_code__gte=500)),
    }


def _latency() -> dict:
    return {
        'avg_latency_ms': Avg('latency_ms'),
        'p95_latency_ms': Percentile('latency_ms', 0.95),
        'max_latency_ms': Max('latency_ms'),
    }


STATUS_CLASS = Case(
    When(status_code=429, then=Value('rate_limited')),
    When(status_code__gte=500, then=Value('server_errors')),
    When(status_code__gte=400, then=Value('client_errors')),
    default=Value('ok'),
    output_field=CharField(),
)


def filtered(since, until, endpoint=None, api_key=None, ip=None, status=None):
    """Raw log rows in [since, until) matching the dashboard filters."""
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


def summary(rows) -> dict:
    totals = rows.aggregate(
        requests=Count('id'),
        unique_ips=Count('client_ip', distinct=True),
        unique_api_keys=Count('api_key', distinct=True),
        **_class_counts(),
        **_latency(),
    )
    return _rounded(totals)


def timeseries(rows, since: datetime, until: datetime, interval: str, split: str) -> dict:
    """Requests per bucket for a chart, every bucket present (zero when empty), in UTC.

    ``split``: ``status`` (one series per status class), ``endpoint`` (the
    busiest endpoints and ``other``) or ``none`` (one series ``all``).
    """
    step = INTERVALS[interval]
    buckets = []
    moment = _floor(since, interval)
    while moment < until:
        buckets.append(moment)
        moment += step
        if len(buckets) > MAX_BUCKETS:
            raise TooManyBuckets
    in_buckets = rows.annotate(bucket=Trunc('created_at', interval, tzinfo=UTC))

    if split == 'status':
        key, names = STATUS_CLASS, list(STATUS_CLASSES)
    elif split == 'endpoint':
        busiest = [
            row['endpoint']
            for row in rows.values('endpoint')
            .annotate(n=Count('id'))
            .order_by('-n', 'endpoint')[:SPLIT_ENDPOINTS]
        ]
        key = Case(
            When(endpoint__in=busiest, then=F('endpoint')),
            default=Value('other'),
            output_field=CharField(),
        )
        names = [*busiest, 'other']
    else:
        key, names = Value('all', output_field=CharField()), ['all']

    counts = defaultdict(dict)
    for row in in_buckets.annotate(key=key).values('bucket', 'key').annotate(n=Count('id')):
        counts[row['key']][row['bucket']] = row['n']
    latency = {
        row['bucket']: row
        for row in in_buckets.values('bucket').annotate(
            avg=Avg('latency_ms'), p95=Percentile('latency_ms', 0.95)
        )
    }
    return {
        'interval': interval,
        'split': split,
        'buckets': [_iso(bucket) for bucket in buckets],
        'series': [
            {'name': name, 'requests': [counts[name].get(bucket, 0) for bucket in buckets]}
            for name in names
            if name != 'other' or counts.get('other')
        ],
        'latency_ms': {
            kind: [_round((latency.get(bucket) or {}).get(kind)) for bucket in buckets]
            for kind in ('avg', 'p95')
        },
    }


def endpoints(rows) -> list[dict]:
    """Requests, errors and latency per endpoint, busiest first."""
    total = rows.count()
    table = (
        rows.values('endpoint')
        .annotate(requests=Count('id'), **_class_counts(), **_latency())
        .order_by('-requests', 'endpoint')
    )
    return [{**_rounded(row), 'share': _share(row['requests'], total)} for row in table]


def top_clients(rows, by: str = 'ip'):
    """Busiest clients by IP address or by API key, as a queryset of dicts (paginate it)."""
    common = {
        'requests': Count('id'),
        **_class_counts(),
        'endpoints': Count('endpoint', distinct=True),
        'first_seen': Min('created_at'),
        'last_seen': Max('created_at'),
        'user_agent': Mode('user_agent'),
    }
    if by == 'api_key':
        grouped = rows.filter(api_key__isnull=False).values(
            'api_key', 'api_key__name', 'api_key__prefix'
        )
        common['ips'] = Count('client_ip', distinct=True)
        order = ['-requests', 'api_key']
    else:
        grouped = rows.values('client_ip')
        common['api_keys'] = Count('api_key', distinct=True)
        order = ['-requests', 'client_ip']
    return grouped.annotate(**common).order_by(*order)


def user_agents(rows):
    """User-Agents by requests, with how many IPs sent each (a queryset of dicts)."""
    return (
        rows.values('user_agent')
        .annotate(requests=Count('id'), ips=Count('client_ip', distinct=True))
        .order_by('-requests', 'user_agent')
    )


def _floor(moment: datetime, interval: str) -> datetime:
    moment = moment.astimezone(UTC).replace(second=0, microsecond=0)
    if interval in ('hour', 'day'):
        moment = moment.replace(minute=0)
    if interval == 'day':
        moment = moment.replace(hour=0)
    return moment


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _round(value):
    return round(value) if value is not None else None


def _rounded(row: dict) -> dict:
    return {
        key: _round(value) if key.endswith('latency_ms') else value for key, value in row.items()
    }


def _share(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


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
        PlannerDailyStat.objects.filter(date__range=(date_from, date_to), **filters).values(
            'date', 'status_code', 'requests', 'latency_ms_sum', 'latency_ms_max'
        )
    )
    # Days not summarised yet (today, and yesterday until the nightly rollup)
    # come from the raw log.
    summarised = set(PlannerDailyStat.objects.values_list('date', flat=True).distinct())
    rows += [
        {**row, 'date': row.pop('created_at__date')}
        for row in PlannerRequestLog.objects.filter(
            created_at__date__range=(date_from, date_to), **filters
        )
        .values('created_at__date', 'status_code')
        .annotate(
            requests=Count('id'),
            latency_ms_sum=Sum('latency_ms'),
            latency_ms_max=Max('latency_ms'),
        )
        if row['created_at__date'] not in summarised
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


def _status_class(code: int) -> str:
    if code == 429:
        return 'rate_limited'
    if code >= 500:
        return 'server_errors'
    if code >= 400:
        return 'client_errors'
    return 'ok'
