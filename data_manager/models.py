from django.db import models
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.conf import settings
from django.contrib.auth import get_user_model

import os


class OverwriteStorage(FileSystemStorage):
    def get_available_name(self, name, max_length=None):
        if self.exists(name):
            os.remove(os.path.join(settings.MEDIA_ROOT, name))
        return name


def _build_base_path(submission) -> str:
    """
    Returns: '{user_id}/{org_id}'
    Falls back to 'unknown' when IDs are not yet set (pre-save).
    """
    user_id = getattr(submission.submitted_by, 'id', None) or 'unknown'
    org_id = submission.transport_organization_id or 'unknown'
    return f'{user_id}/{org_id}'


def static_feed_file_upload_to(instance, filename):
    """
    For files uploaded directly by the user.
    {user_id}/{org_id}/static/{original_filename}
    """
    base = _build_base_path(instance.submission)
    return f'{base}/static/{filename}'


def static_feed_cached_upload_to(instance, filename):
    """
    For files downloaded automatically by the server (hide_original=True).
    {user_id}/{org_id}/static/{original_filename}
    """
    base = _build_base_path(instance.submission)
    return f'{base}/static/{filename}'


def realtime_feed_cached_upload_to(instance, filename):
    """
    For realtime feed files downloaded automatically by the server (hide_original=True).
    {user_id}/{org_id}/realtime/{endpoint_type}/{original_filename}
    """
    entry = instance.entry
    submission = entry.submission
    base = _build_base_path(submission)
    endpoint_type = instance.endpoint_type or 'unknown'
    return f'{base}/realtime/{endpoint_type}/{filename}'


# ---------------------------------------------------------------------------
# FeedSubmission – top-level submission by a user
# ---------------------------------------------------------------------------

class FeedSubmission(models.Model):
    DATA_TYPE_CHOICES = [
        # Static
        ('gtfs', 'GTFS'),
        ('netex', 'NeTEx'),
        # Dynamic
        ('gbfs', 'GBFS'),
        ('siri', 'SIRI'),
        ('gtfs_rt', 'GTFS-RT'),
        # Other
        ('other', 'Other'),
    ]

    FEED_KIND_STATIC = 'static'
    FEED_KIND_DYNAMIC = 'dynamic'
    FEED_KIND_CHOICES = [
        (FEED_KIND_STATIC, 'Static'),
        (FEED_KIND_DYNAMIC, 'Dynamic'),
    ]

    DYNAMIC_DATA_TYPES = {'gtfs_rt', 'siri', 'gbfs'}

    # Relations
    transport_organization = models.ForeignKey(
        'cases.TransportOrganization',
        related_name='feed_submissions',
        on_delete=models.CASCADE,
    )
    submitted_by = models.ForeignKey(
        get_user_model(),
        related_name='feed_submissions',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    # Feed metadata
    data_type = models.CharField(max_length=10, choices=DATA_TYPE_CHOICES)
    feed_kind = models.CharField(max_length=10, choices=FEED_KIND_CHOICES, editable=False)
    name = models.CharField(max_length=255, blank=True, null=True)
    note = models.TextField(max_length=2048, blank=True, null=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['transport_organization', '-created_at']),
            models.Index(fields=['data_type']),
            models.Index(fields=['feed_kind']),
        ]

    # ------------------------------------------------------------------
    # Computed properties – derived from FeedSubmissionHistory
    # ------------------------------------------------------------------

    @property
    def current_stage(self) -> int:
        """Returns the current stage (1–4) based on the latest history entry."""
        latest = self.history.order_by('-created_at').first()
        if latest is None:
            return 1
        return latest.stage_after

    @property
    def current_stage_label(self) -> str:
        if self.is_rejected:
            return 'Rejected'
        labels = {
            1: 'Step 1: Upload data',
            2: 'Step 2: Data verification',
            3: 'Step 3: Admin confirmation',
            4: 'Step 4: Complete',
        }
        return labels.get(self.current_stage, 'Unknown')

    @property
    def is_rejected(self) -> bool:
        """True if the latest history entry is a rejection."""
        latest = self.history.order_by('-created_at').first()
        return latest is not None and latest.event_type == FeedSubmissionHistory.EVENT_REJECTED

    @property
    def rejection_cause(self):
        """Returns cause from last rejected history entry, or None."""
        if not self.is_rejected:
            return None
        latest = self.history.order_by('-created_at').first()
        return latest.cause if latest else None

    @property
    def published_at(self):
        """Returns created_at from the 'completed' history entry, or None."""
        completed = self.history.filter(
            event_type=FeedSubmissionHistory.EVENT_COMPLETED
        ).order_by('-created_at').first()
        return completed.created_at if completed else None

    def clean(self):
        super().clean()
        # Derive feed_kind from data_type automatically
        if self.data_type in self.DYNAMIC_DATA_TYPES:
            self.feed_kind = self.FEED_KIND_DYNAMIC
        else:
            self.feed_kind = self.FEED_KIND_STATIC

    def save(self, *args, **kwargs):
        # Set feed_kind before saving (clean may not always be called via full_clean)
        if self.data_type in self.DYNAMIC_DATA_TYPES:
            self.feed_kind = self.FEED_KIND_DYNAMIC
        else:
            self.feed_kind = self.FEED_KIND_STATIC
        super().save(*args, **kwargs)

    def __str__(self):
        label = self.name or f"org_id={self.transport_organization_id}"
        return f"FeedSubmission(id={self.id}, {self.data_type}, {label})"


# ---------------------------------------------------------------------------
# FeedSubmissionHistory – audit trail, sole source of truth for stages
# ---------------------------------------------------------------------------

class FeedSubmissionHistory(models.Model):
    EVENT_UPLOADED = 'uploaded'
    EVENT_STAGE_ADVANCED = 'stage_advanced'
    EVENT_REJECTED = 'rejected'
    EVENT_COMPLETED = 'completed'

    EVENT_TYPE_CHOICES = [
        (EVENT_UPLOADED, 'Uploaded'),
        (EVENT_STAGE_ADVANCED, 'Stage Advanced'),
        (EVENT_REJECTED, 'Rejected'),
        (EVENT_COMPLETED, 'Completed'),
    ]

    submission = models.ForeignKey(
        FeedSubmission,
        on_delete=models.CASCADE,
        related_name='history',
    )
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    stage_before = models.IntegerField()
    stage_after = models.IntegerField()
    actor = models.ForeignKey(
        get_user_model(),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feed_history_actions',
    )
    cause = models.TextField(
        blank=True,
        null=True,
        help_text='Reason for rejection — only filled for event_type=rejected',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Feed Submission History'
        verbose_name_plural = 'Feed Submission Histories'

    def __str__(self):
        return (
            f"FeedSubmissionHistory(submission={self.submission_id}, "
            f"event={self.event_type}, {self.stage_before}→{self.stage_after})"
        )


# ---------------------------------------------------------------------------
# StaticFeedEntry – for static feeds (GTFS, NeTEx, other)
# ---------------------------------------------------------------------------

class StaticFeedEntry(models.Model):
    AUTH_TYPE_CHOICES = [
        ('none', 'None'),
        ('api_key', 'API Key'),
        ('bearer_token', 'Bearer Token'),
        ('basic_auth', 'Basic Auth (user:pass)'),
    ]

    submission = models.ForeignKey(
        FeedSubmission,
        on_delete=models.CASCADE,
        related_name='static_entries',
    )

    license = models.CharField(max_length=255, blank=True, null=True)

    # Source A: URL – server fetches periodically when hide_original=True,
    #           or exposed directly when hide_original=False.
    url = models.URLField(blank=True, null=True, help_text='URL to the static feed file.')

    # Source B: file uploaded manually by the user.
    file = models.FileField(
        upload_to=static_feed_file_upload_to,
        storage=OverwriteStorage(),
        blank=True,
        null=True,
        help_text='Static feed file uploaded directly by the user.',
    )

    # is_original – whether this agency is the original author of the feed
    is_original = models.BooleanField(
        default=False,
        help_text='Whether this agency is the original author of the feed.',
    )

    # Source C: file downloaded automatically by the server (filled only when
    #           url is set and hide_original=True — never set by the user).
    cached_file = models.FileField(
        upload_to=static_feed_cached_upload_to,
        storage=OverwriteStorage(),
        blank=True,
        null=True,
        help_text='Server-cached copy of the feed (set automatically, never by the user).',
    )
    cached_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the cached copy was last downloaded by the server.',
    )

    # Validation Report
    # Stores the last validation result (if any).
    validation_report = models.OneToOneField(
        'FeedValidationReport',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='static_entry',
    )

    # Proxy/hide original URL (only relevant when url is set)
    hide_original = models.BooleanField(
        default=False,
        help_text='If checked, the server fetches the file and serves it instead of exposing the original URL.',
    )

    # Authentication (used by the server when fetching the URL)
    auth_type = models.CharField(
        max_length=20,
        choices=AUTH_TYPE_CHOICES,
        default='none',
    )
    auth_value = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text='API key, bearer token, or "username:password" for basic auth.',
    )

    # Daily download schedule (only when url + hide_original=True)
    download_time_1 = models.TimeField(
        null=True,
        blank=True,
        help_text='Time of day (UTC) to download and cache the feed.',
    )
    download_time_2 = models.TimeField(
        null=True,
        blank=True,
        help_text='Optional second daily download time (for feeds updated twice a day).',
    )

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def clean(self):
        super().clean()
        has_url = bool(self.url)
        has_file = bool(self.file)

        # User chooses exactly one input source: url XOR file.
        if has_url and has_file:
            raise ValidationError('Provide either a URL or a file upload, not both.')
        if not has_url and not has_file:
            raise ValidationError('Either a URL or a file upload must be provided.')

        # Auto-set hide_original when auth_type != none
        if self.auth_type != 'none':
            self.hide_original = True

        # URL-specific rules
        if has_url:
            if self.hide_original and self.auth_type == 'none':
                raise ValidationError(
                    'Authentication is required when "hide original" is enabled.'
                )
        else:
            # file upload – URL-only fields must not be filled
            if self.hide_original:
                raise ValidationError(
                    '"Hide original" is only applicable when a URL is provided, not a file upload.'
                )
            if self.download_time_1 or self.download_time_2:
                raise ValidationError(
                    'Download schedule is only applicable when a URL is provided, not a file upload.'
                )
            if self.cached_file or self.cached_at:
                raise ValidationError(
                    'Server-downloaded static file fields cannot be used for a manual file upload.'
                )

    def __str__(self):
        source = self.url or getattr(self.file, 'name', '')
        return f"StaticFeedEntry(id={self.id}, submission={self.submission_id}, source='{source}')"


# ---------------------------------------------------------------------------
# FeedValidationReport – stores output of the GTFS validator
# ---------------------------------------------------------------------------

class FeedValidationReport(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    report_json = models.JSONField(
        blank=True,
        null=True,
        help_text='Full JSON output from the validator.'
    )
    error_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"ValidationReport(errors={self.error_count}, warnings={self.warning_count})"


# ---------------------------------------------------------------------------
# RealtimeFeedEntry – shared container for GTFS-RT, SIRI, and GBFS
# ---------------------------------------------------------------------------

class RealtimeFeedEntry(models.Model):
    PROTOCOL_GTFS_RT = 'gtfs_rt'
    PROTOCOL_SIRI = 'siri'
    PROTOCOL_GBFS = 'gbfs'
    PROTOCOL_CHOICES = [
        (PROTOCOL_GTFS_RT, 'GTFS-RT'),
        (PROTOCOL_SIRI, 'SIRI'),
        (PROTOCOL_GBFS, 'GBFS'),
    ]

    # Endpoint types per protocol
    GTFS_RT_ENDPOINT_TYPES = {'trip_update', 'vehicle_position', 'service_alert'}
    SIRI_ENDPOINT_TYPES = {'sx', 'sm', 'vm', 'et', 'gm'}
    GBFS_ENDPOINT_TYPES = {
        'gbfs', 'gbfs_versions', 'system_information', 'vehicle_types',
        'station_information', 'station_status', 'free_bike_status',
        'system_hours', 'system_alerts',
    }

    license = models.CharField(max_length=255, blank=True, null=True)

    submission = models.OneToOneField(
        FeedSubmission,
        on_delete=models.CASCADE,
        related_name='realtime_entry',
    )
    protocol = models.CharField(max_length=10, choices=PROTOCOL_CHOICES)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def allowed_endpoint_types(self) -> set:
        if self.protocol == self.PROTOCOL_GTFS_RT:
            return self.GTFS_RT_ENDPOINT_TYPES
        if self.protocol == self.PROTOCOL_SIRI:
            return self.SIRI_ENDPOINT_TYPES
        if self.protocol == self.PROTOCOL_GBFS:
            return self.GBFS_ENDPOINT_TYPES
        return set()

    def clean(self):
        super().clean()
        # Cross-check: protocol must match submission data_type
        if self.submission_id:
            expected = self.submission.data_type
            if expected != self.protocol:
                raise ValidationError(
                    f"Protocol '{self.protocol}' does not match submission data_type '{expected}'."
                )

    def __str__(self):
        return f"RealtimeFeedEntry(id={self.id}, protocol={self.protocol}, submission={self.submission_id})"


# ---------------------------------------------------------------------------
# RealtimeEndpoint – one URL per endpoint type within a RealtimeFeedEntry
# ---------------------------------------------------------------------------

class RealtimeEndpoint(models.Model):
    AUTH_TYPE_CHOICES = [
        ('none', 'None'),
        ('api_key', 'API Key'),
        ('bearer_token', 'Bearer Token'),
        ('basic_auth', 'Basic Auth (user:pass)'),
    ]

    # GTFS-RT endpoint types
    ENDPOINT_TRIP_UPDATE = 'trip_update'
    ENDPOINT_VEHICLE_POSITION = 'vehicle_position'
    ENDPOINT_SERVICE_ALERT = 'service_alert'
    # SIRI endpoint types
    ENDPOINT_SX = 'sx'
    ENDPOINT_SM = 'sm'
    ENDPOINT_VM = 'vm'
    ENDPOINT_ET = 'et'
    ENDPOINT_GM = 'gm'
    # GBFS endpoint types
    ENDPOINT_GBFS = 'gbfs'
    ENDPOINT_GBFS_VERSIONS = 'gbfs_versions'
    ENDPOINT_SYSTEM_INFORMATION = 'system_information'
    ENDPOINT_VEHICLE_TYPES = 'vehicle_types'
    ENDPOINT_STATION_INFORMATION = 'station_information'
    ENDPOINT_STATION_STATUS = 'station_status'
    ENDPOINT_FREE_BIKE_STATUS = 'free_bike_status'
    ENDPOINT_SYSTEM_HOURS = 'system_hours'
    ENDPOINT_SYSTEM_ALERTS = 'system_alerts'

    ENDPOINT_TYPE_CHOICES = [
        # GTFS-RT
        (ENDPOINT_TRIP_UPDATE, 'Trip Update (GTFS-RT)'),
        (ENDPOINT_VEHICLE_POSITION, 'Vehicle Position (GTFS-RT)'),
        (ENDPOINT_SERVICE_ALERT, 'Service Alert (GTFS-RT)'),
        # SIRI
        (ENDPOINT_SX, 'Situation Exchange – SX (SIRI)'),
        (ENDPOINT_SM, 'Stop Monitoring – SM (SIRI)'),
        (ENDPOINT_VM, 'Vehicle Monitoring – VM (SIRI)'),
        (ENDPOINT_ET, 'Estimated Timetable – ET (SIRI)'),
        (ENDPOINT_GM, 'General Message – GM (SIRI)'),
        # GBFS
        (ENDPOINT_GBFS, 'GBFS – Main file'),
        (ENDPOINT_GBFS_VERSIONS, 'GBFS Versions'),
        (ENDPOINT_SYSTEM_INFORMATION, 'System Information'),
        (ENDPOINT_VEHICLE_TYPES, 'Vehicle Types'),
        (ENDPOINT_STATION_INFORMATION, 'Station Information'),
        (ENDPOINT_STATION_STATUS, 'Station Status'),
        (ENDPOINT_FREE_BIKE_STATUS, 'Free Bike Status'),
        (ENDPOINT_SYSTEM_HOURS, 'System Hours'),
        (ENDPOINT_SYSTEM_ALERTS, 'System Alerts'),
    ]

    entry = models.ForeignKey(
        RealtimeFeedEntry,
        on_delete=models.CASCADE,
        related_name='endpoints',
    )
    endpoint_type = models.CharField(max_length=30, choices=ENDPOINT_TYPE_CHOICES)
    url = models.URLField()
    hide_original = models.BooleanField(
        default=False,
        help_text='Server acts as a proxy and hides the original URL.',
    )
    is_original = models.BooleanField(
        default=False,
        help_text='Whether this agency is the original author of the feed.',
    )
    # Populated automatically when server fetches the feed (hide_original=True)
    cached_file = models.FileField(
        upload_to=realtime_feed_cached_upload_to,
        storage=OverwriteStorage(),
        blank=True,
        null=True,
        help_text='Server-cached copy of the feed (filled automatically).',
    )
    cached_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the cached copy was last downloaded.',
    )
    interval = models.PositiveIntegerField(
        help_text='Refresh interval in seconds (e.g. 30, 60). Required.',
    )
    auth_type = models.CharField(
        max_length=20,
        choices=AUTH_TYPE_CHOICES,
        default='none',
    )
    auth_value = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text='API key, bearer token, or "username:password" for basic auth.',
    )

    class Meta:
        # One endpoint_type per entry (e.g. only one trip_update per submission)
        unique_together = [('entry', 'endpoint_type')]
        ordering = ['entry', 'endpoint_type']

    def clean(self):
        super().clean()
        # Auto-set hide_original when auth_type != none
        if self.auth_type != 'none':
            self.hide_original = True

        # Validate endpoint_type is allowed for the parent protocol
        if self.entry_id:
            allowed = self.entry.allowed_endpoint_types()
            if self.endpoint_type not in allowed:
                raise ValidationError(
                    f"Endpoint type '{self.endpoint_type}' is not valid for "
                    f"protocol '{self.entry.protocol}'. Allowed: {sorted(allowed)}."
                )
        if self.hide_original and self.auth_type == 'none':
            raise ValidationError(
                'Authentication is required when "hide original" is enabled.'
            )

    def __str__(self):
        return (
            f"RealtimeEndpoint(id={self.id}, type={self.endpoint_type}, "
            f"entry={self.entry_id})"
        )


# ---------------------------------------------------------------------------
# FeedFetchError – immutable error log for feed download failures
# ---------------------------------------------------------------------------

class FeedFetchError(models.Model):
    ERROR_HTTP = 'http_error'
    ERROR_TIMEOUT = 'timeout'
    ERROR_CONNECTION = 'connection_error'
    ERROR_INVALID_CONTENT = 'invalid_content'
    ERROR_AUTH = 'auth_error'

    ERROR_TYPE_CHOICES = [
        (ERROR_HTTP, 'HTTP Error (4xx/5xx)'),
        (ERROR_TIMEOUT, 'Timeout'),
        (ERROR_CONNECTION, 'Connection Error'),
        (ERROR_INVALID_CONTENT, 'Invalid Content'),
        (ERROR_AUTH, 'Authentication Error'),
    ]

    static_entry = models.ForeignKey(
        StaticFeedEntry,
        on_delete=models.CASCADE,
        related_name='fetch_errors',
        null=True,
        blank=True,
    )
    endpoint = models.ForeignKey(
        RealtimeEndpoint,
        on_delete=models.CASCADE,
        related_name='fetch_errors',
        null=True,
        blank=True,
    )
    error_type = models.CharField(max_length=20, choices=ERROR_TYPE_CHOICES)
    http_status_code = models.IntegerField(
        null=True, blank=True,
        help_text='HTTP response code — filled only for error_type=http_error',
    )
    message = models.TextField(help_text='Detailed error message or exception description.')
    url_attempted = models.URLField(help_text='URL from which the file was attempted to download.')
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at']
        verbose_name = 'Feed Fetch Error'
        verbose_name_plural = 'Feed Fetch Errors'

    def clean(self):
        super().clean()
        has_static = bool(self.static_entry_id)
        has_endpoint = bool(self.endpoint_id)
        if has_static and has_endpoint:
            raise ValidationError(
                'A FeedFetchError must be linked to either a StaticFeedEntry or a RealtimeEndpoint, not both.'
            )
        if not has_static and not has_endpoint:
            raise ValidationError(
                'A FeedFetchError must be linked to either a StaticFeedEntry or a RealtimeEndpoint.'
            )

    def __str__(self):
        source = f'static_entry={self.static_entry_id}' if self.static_entry_id else f'endpoint={self.endpoint_id}'
        return f"FeedFetchError(id={self.id}, {source}, {self.error_type}, {self.occurred_at})"

# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------

def completed_submission_ids() -> list[int]:
    """Returns IDs of all submissions that are in 'completed' stage (4)."""
    # Assuming stage 4 is the final 'published' stage.
    # We can optimize by querying history: find submissions where latest history entry is stage_after=4.
    # However, a simpler way for now:
    # Filter submissions where current_stage property returns 4.
    # Since current_stage is a property computed from history, doing it in Python for all might be slow.
    # Better: Use a subquery or annotation if possible.
    # But let's stick to the simplest correct implementation:

    # We can fetch IDs of submissions that have *ever* reached stage 4,
    # and haven't been reverted/rejected later?
    # Actually, let's keep it simple: fetch all submissions and filter in python if count is low,
    # or rely on a specific optimized query.

    # Optimized query:
    # Submissions where the latest history entry has stage_after=4.

    from django.db.models import Subquery, OuterRef

    latest_history = FeedSubmissionHistory.objects.filter(
        submission=OuterRef('pk')
    ).order_by('-created_at')

    # This filters submissions where the *very last* history entry is stage 4.
    return list(
        FeedSubmission.objects.annotate(
            current_stage_val=Subquery(latest_history.values('stage_after')[:1])
        ).filter(
            current_stage_val=4
        ).values_list('id', flat=True)
    )
