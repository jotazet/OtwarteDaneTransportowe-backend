"""Journey planner: which ODT feeds go into the OpenTripPlanner graph, and graph builds.

See docs/architecture/decisions/0008 and 0009 and docs/domain/journey-planner.md.
"""

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q


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
