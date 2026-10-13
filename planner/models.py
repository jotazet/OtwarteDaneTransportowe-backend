"""Journey planner: which ODT feeds go into the OpenTripPlanner graph, and graph builds.

See docs/architecture/decisions/0008 and 0009 and docs/domain/journey-planner.md.
"""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import BrinIndex, GinIndex
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class PlannerFeed(models.Model):
    """A published static GTFS feed that the admin included in the planner graph."""

    POLICY_KEEP_LAST_GOOD = 'keep_last_good'
    POLICY_EXCLUDE = 'exclude'
    FAILURE_POLICY_CHOICES = [
        (POLICY_KEEP_LAST_GOOD, 'Keep the last good version'),
        (POLICY_EXCLUDE, 'Exclude the feed'),
    ]

    submission = models.OneToOneField(
        'data_manager.FeedSubmission',
        on_delete=models.CASCADE,
        related_name='planner_feed',
    )
    # OTP's feedId: the prefix of every stop, route and trip ID the planner API
    # exposes, so it never changes once created.
    feed_id = models.CharField(
        max_length=32,
        unique=True,
        validators=[RegexValidator(r'^[a-z0-9][a-z0-9-]{1,31}$', code='planner.invalid_feed_id')],
    )
    is_enabled = models.BooleanField(default=True)
    include_realtime = models.BooleanField(default=True)
    failure_policy = models.CharField(
        max_length=16, choices=FAILURE_POLICY_CHOICES, default=POLICY_KEEP_LAST_GOOD
    )
    # The last file that passed a build, kept under PLANNER_DATA_DIR/snapshots/.
    last_good_sha256 = models.CharField(max_length=64, blank=True, default='')
    last_good_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['feed_id']

    def __str__(self):
        return f'PlannerFeed({self.feed_id}, submission={self.submission_id})'


class GraphBuild(models.Model):
    STATUS_QUEUED = 'queued'
    STATUS_RUNNING = 'running'
    STATUS_SUCCEEDED = 'succeeded'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_QUEUED, 'Queued'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_SUCCEEDED, 'Succeeded'),
        (STATUS_FAILED, 'Failed'),
    ]
    ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

    TRIGGER_SCHEDULE = 'schedule'
    TRIGGER_MANUAL = 'manual'
    TRIGGER_ROLLBACK = 'rollback'
    TRIGGER_CHOICES = [
        (TRIGGER_SCHEDULE, 'Schedule'),
        (TRIGGER_MANUAL, 'Manual'),
        (TRIGGER_ROLLBACK, 'Rollback'),
    ]

    OUTCOME_FRESH = 'fresh'
    OUTCOME_STALE = 'stale'
    OUTCOME_EXCLUDED = 'excluded'

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True
    )
    trigger = models.CharField(max_length=16, choices=TRIGGER_CHOICES)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    # A rollback re-serves this earlier build's graph instead of building one.
    rollback_of = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True)
    otp_image = models.CharField(max_length=255)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    # Set while the serving OTP is stopped: the planner is in its maintenance window.
    otp_stopped_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=False)
    # When the search index was copied from this build's graph (None: not yet).
    indexed_at = models.DateTimeField(null=True, blank=True)

    # One entry per planner feed: planner_feed, feed_id, submission, outcome,
    # sha256, version_at, error_code, log_excerpt, realtime_skipped.
    feed_results = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default='')
    log_excerpt = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['is_active'],
                condition=Q(is_active=True),
                name='planner_one_active_graph_build',
            ),
        ]

    def __str__(self):
        return f'GraphBuild(id={self.id}, {self.status})'


# ---------------------------------------------------------------------------
# Public API access: settings, keys, bans, request log (ADR 0010)
# ---------------------------------------------------------------------------

LIMIT_GROUPS = ('plan', 'realtime', 'lookup')


class PlannerSettings(models.Model):
    """The single row of runtime settings for the public planner API (pk is always 1).

    Limits are requests per minute, per client (an IP, or an IPv6 /64) or per
    API key, and for all clients together. The defaults are starting values,
    to be tuned with the request statistics.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    public_api_enabled = models.BooleanField(default=False)

    anonymous_plan_per_minute = models.PositiveIntegerField(default=20)
    anonymous_realtime_per_minute = models.PositiveIntegerField(default=120)
    anonymous_lookup_per_minute = models.PositiveIntegerField(default=300)
    key_plan_per_minute = models.PositiveIntegerField(default=120)
    key_realtime_per_minute = models.PositiveIntegerField(default=600)
    key_lookup_per_minute = models.PositiveIntegerField(default=1200)
    global_plan_per_minute = models.PositiveIntegerField(default=300)
    global_realtime_per_minute = models.PositiveIntegerField(default=3000)
    global_lookup_per_minute = models.PositiveIntegerField(default=6000)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name='planner_settings_single_row'),
        ]

    def __str__(self):
        return 'PlannerSettings'

    @classmethod
    def load(cls) -> 'PlannerSettings':
        return cls.objects.get_or_create(pk=1)[0]

    def limit(self, client_class: str, group: str) -> int:
        return getattr(self, f'{client_class}_{group}_per_minute')


class ApiKey(models.Model):
    """A key an admin issued to a developer; only its SHA-256 is stored."""

    name = models.CharField(max_length=100)
    contact_email = models.EmailField(blank=True)
    note = models.TextField(blank=True)
    # The first characters of the key, shown so admins can tell keys apart.
    prefix = models.CharField(max_length=12, unique=True)
    key_hash = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    # Per-key limits; null means the key limits in PlannerSettings.
    plan_per_minute = models.PositiveIntegerField(null=True, blank=True)
    realtime_per_minute = models.PositiveIntegerField(null=True, blank=True)
    lookup_per_minute = models.PositiveIntegerField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name', 'id']

    def __str__(self):
        return f'ApiKey({self.prefix}…, {self.name})'


class IpBan(models.Model):
    """A blocked address or network, e.g. 203.0.113.7/32 or 2001:db8:1:2::/64."""

    network = models.CharField(max_length=49, unique=True)
    reason = models.CharField(max_length=255, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'IpBan({self.network})'


class PlannerRequestLog(models.Model):
    """One request to the public planner API. Kept 30 days; never the query string."""

    created_at = models.DateTimeField(default=timezone.now)
    endpoint = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField()
    latency_ms = models.PositiveIntegerField()
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    api_key = models.ForeignKey(ApiKey, on_delete=models.SET_NULL, null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [BrinIndex(fields=['created_at'], name='planner_request_log_time')]

    def __str__(self):
        return f'PlannerRequestLog({self.endpoint}, {self.status_code})'


class PlannerDailyStat(models.Model):
    """Requests per day, endpoint, key and status: what remains after the logs expire."""

    date = models.DateField()
    endpoint = models.CharField(max_length=64)
    api_key = models.ForeignKey(ApiKey, on_delete=models.SET_NULL, null=True, blank=True)
    status_code = models.PositiveSmallIntegerField()
    requests = models.PositiveIntegerField()
    latency_ms_sum = models.BigIntegerField()
    latency_ms_max = models.PositiveIntegerField()

    class Meta:
        ordering = ['-date', 'endpoint']
        constraints = [
            models.UniqueConstraint(
                fields=['date', 'endpoint', 'api_key', 'status_code'],
                name='planner_daily_stat_unique',
                nulls_distinct=False,
            ),
        ]
        indexes = [models.Index(fields=['date'], name='planner_daily_stat_date')]

    def __str__(self):
        return f'PlannerDailyStat({self.date}, {self.endpoint}, {self.status_code})'


# ---------------------------------------------------------------------------
# Search index: agencies, lines and stops of the served graph, copied from OTP
# after every activation (planner/search_index.py). Replaced as a whole, never edited.
# Text is unbounded: lengths come from the feeds, and one long value must not
# stop the index of every feed.
# ---------------------------------------------------------------------------


class IndexedAgency(models.Model):
    agency_id = models.TextField(unique=True)  # feed-scoped, e.g. krk:302
    feed_id = models.TextField()
    name = models.TextField()
    url = models.TextField(blank=True)
    organization = models.ForeignKey(
        'cases.TransportOrganization',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['name', 'agency_id']

    def __str__(self):
        return self.agency_id


class IndexedRoute(models.Model):
    route_id = models.TextField(unique=True)
    agency = models.ForeignKey(IndexedAgency, on_delete=models.CASCADE, related_name='routes')
    short_name = models.TextField(blank=True)
    long_name = models.TextField(blank=True)
    mode = models.TextField()
    color = models.TextField(blank=True)
    text_color = models.TextField(blank=True)
    # Lines in timetable order: "2" before "10" before "A".
    sort_key = models.TextField()
    short_name_search = models.TextField()
    name_search = models.TextField()
    # Centre of the line's stops, to rank lines near the user first.
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['sort_key', 'route_id']
        indexes = [
            models.Index(fields=['short_name_search'], name='planner_route_short_name'),
            GinIndex(
                fields=['name_search'], opclasses=['gin_trgm_ops'], name='planner_route_name_trgm'
            ),
        ]

    def __str__(self):
        return self.route_id


class IndexedStopGroup(models.Model):
    """Stops with the same name close together, e.g. every pole of "Rondo Mogilskie".

    Stop search returns groups, not single poles; few feeds group poles under a
    GTFS parent station (docs/domain/journey-planner.md).
    """

    name = models.TextField()
    name_search = models.TextField()
    lat = models.FloatField()
    lon = models.FloatField()
    modes = ArrayField(models.TextField(), default=list)
    agency_ids = ArrayField(models.TextField(), default=list)
    route_ids = ArrayField(models.TextField(), default=list)
    route_count = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            GinIndex(
                fields=['name_search'], opclasses=['gin_trgm_ops'], name='planner_group_name_trgm'
            ),
            GinIndex(fields=['agency_ids'], name='planner_group_agencies'),
            models.Index(fields=['lat', 'lon'], name='planner_group_position'),
        ]

    def __str__(self):
        return self.name


class IndexedStop(models.Model):
    stop_id = models.TextField(unique=True)
    group = models.ForeignKey(IndexedStopGroup, on_delete=models.CASCADE, related_name='stops')
    name = models.TextField()
    code = models.TextField(blank=True)
    platform = models.TextField(blank=True)
    lat = models.FloatField()
    lon = models.FloatField()
    mode = models.TextField(blank=True)
    station_id = models.TextField(blank=True)
    route_ids = ArrayField(models.TextField(), default=list)

    class Meta:
        ordering = ['stop_id']
        indexes = [models.Index(fields=['lat', 'lon'], name='planner_stop_position')]

    def __str__(self):
        return self.stop_id


class IndexedBlockTrip(models.Model):
    """A trip that belongs to a block (GTFS block_id): one vehicle's trips of a day.

    OTP cannot list a block's trips, so the index keeps which trips share one.
    """

    feed_id = models.TextField()
    block_id = models.TextField()
    trip_id = models.TextField(unique=True)

    class Meta:
        indexes = [models.Index(fields=['feed_id', 'block_id'], name='planner_block_trip_block')]

    def __str__(self):
        return f'{self.feed_id}:{self.block_id} {self.trip_id}'
