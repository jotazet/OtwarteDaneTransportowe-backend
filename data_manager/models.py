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


def feed_file_path(instance, filename):
    """
    file will be uploaded to MEDIA_ROOT/<submission_id>/static/<filename>
    """
    submission_id = getattr(getattr(instance, 'submission', None), 'id', 'unknown')
    return f'{submission_id}/static/{filename}'


def feed_cached_file_path(instance, filename):
    """Return path for cached static feed files.

    Files are stored alongside other static submission files under::

        MEDIA_ROOT/<submission_id>/static/cached/<filename>

    This mirrors the structure used for dynamic feeds in
    ``realtime_feed_cached_file_path`` but for static feeds ``instance``
    is a ``StaticFeedEntry`` and has a direct ``submission`` FK.
    """
    submission_id = getattr(getattr(instance, 'submission', None), 'id', 'unknown')
    return f'{submission_id}/static/cached/{filename}'


def realtime_feed_cached_file_path(instance, filename):
    """
    MEDIA_ROOT/<realtime_submission_id>/dynamic/cached/<filename>
    """
    submission_id = getattr(getattr(instance, 'submission', None), 'id', 'unknown')
    return f'{submission_id}/dynamic/cached/{filename}'


def validation_file_path(instance, filename):
    """
    file will be uploaded to MEDIA_ROOT/<submission_id>/validation/<filename>
    """
    submission_id = getattr(getattr(getattr(instance, 'static_entry', None), 'submission', None), 'id', 'unknown')
    return f'{submission_id}/validation/{filename}'

# ---------------------------------------------------------------------------
# FeedSubmission – top-level submission by a user
# ---------------------------------------------------------------------------

class FeedSubmission(models.Model):
    """Static schedule feeds only. Realtime uses :class:`RealtimeSubmission`."""

    DATA_TYPE_CHOICES = [
        ('gtfs', 'GTFS'),
        ('netex', 'NeTEx'),
        ('other', 'Other'),
    ]

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

    # NOTE: realtime is handled via separate RealtimeSubmission flow
    # (see data_manager.models.RealtimeSubmission).

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
        ('api_key', 'API Key'),
        ('bearer_token', 'Bearer Token'),
        ('basic_auth', 'Basic Auth (user:pass)'),
    ]

    VALIDATION_PENDING = 'pending'
    VALIDATION_VALID = 'valid'
    VALIDATION_INVALID = 'invalid'
    VALIDATION_ERROR = 'error'
    VALIDATION_STATUS_CHOICES = [
        (VALIDATION_PENDING, 'Pending'),
        (VALIDATION_VALID, 'Valid'),
        (VALIDATION_INVALID, 'Invalid'),
        (VALIDATION_ERROR, 'Error'),
    ]

    submission = models.OneToOneField(
        FeedSubmission,
        on_delete=models.CASCADE,
        related_name='static_entry',
    )

    license = models.CharField(max_length=255, blank=True, null=True)

    # Source A: URL – server fetches periodically when hide_original=True,
    #           or exposed directly when hide_original=False.
    url = models.URLField(blank=True, null=True, help_text='URL to the static feed file.')

    # Source B: file uploaded manually by the user.
    file = models.FileField(
        upload_to=feed_file_path,
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
        upload_to=feed_cached_file_path,
        storage=OverwriteStorage(),
        blank=True,
        null=True,
        help_text='Server-cached copy of the feed (set automatically, never by the user).',
    )
    cached_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the cached copy was last downloaded by the server.',
    )

    validation_status = models.CharField(
        max_length=20,
        choices=VALIDATION_STATUS_CHOICES,
        default=VALIDATION_PENDING,
    )
    validation_message = models.TextField(blank=True, null=True)

    # Validation Report
    # Stores the last validation result (if any).
    validation_report = models.OneToOneField(
        'FeedValidationReport',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='static_entry_report',
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
        null=True, blank=True
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

        # download_time_1 required if url, forbidden if file
        if has_url:
            if self.download_time_1 is None:
                raise ValidationError({'download_time_1': 'This field is required when a URL is provided.'})
            if self.download_time_2 is not None and self.download_time_1 is None:
                raise ValidationError({'download_time_2': 'download_time_2 can only be set if download_time_1 is set.'})
        if has_file:
            if self.download_time_1 is not None:
                raise ValidationError({'download_time_1': 'This field must be empty when a file is provided.'})
            if self.download_time_2 is not None:
                raise ValidationError({'download_time_2': 'This field must be empty when a file is provided.'})

        # Auto-set hide_original when auth_type is set
        if self.auth_type:
            self.hide_original = True

        # URL-specific rules
        if has_url:
            if self.hide_original and not self.auth_type:
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

    @property
    def validation_source_file(self):
        return self.cached_file or self.file

    def validation_ready(self) -> bool:
        return bool(self.validation_source_file)

    def cleanup_validation_artifacts(self):
        if self.validation_report and self.validation_report.report_file:
            storage = self.validation_report.report_file.storage
            name = self.validation_report.report_file.name
            if name and storage.exists(name):
                storage.delete(name)


# ---------------------------------------------------------------------------
# FeedValidationReport – stores output of the GTFS validator
# ---------------------------------------------------------------------------

class FeedValidationReport(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    report_file = models.FileField(
        upload_to=validation_file_path,
        storage=OverwriteStorage(),
        blank=True,
        null=True,
        help_text='Full JSON output from the validator, stored as a file.'
    )
    error_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"ValidationReport(errors={self.error_count}, warnings={self.warning_count})"


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
    endpoint_rt = models.ForeignKey(
        'RealtimeEndpointRT',
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
        has_endpoint = bool(self.endpoint_rt_id)
        if has_static and has_endpoint:
            raise ValidationError(
                'A FeedFetchError must be linked to either a StaticFeedEntry or a RealtimeEndpointRT, not both.'
            )
        if not has_static and not has_endpoint:
            raise ValidationError(
                'A FeedFetchError must be linked to either a StaticFeedEntry or a RealtimeEndpointRT.'
            )

    def __str__(self):
        source = f'static_entry={self.static_entry_id}' if self.static_entry_id else f'endpoint_rt={self.endpoint_rt_id}'
        return f"FeedFetchError(id={self.id}, {source}, {self.error_type}, {self.occurred_at})"


# ---------------------------------------------------------------------------
# RealtimeSubmission – realtime flow attached to a published static submission
# ---------------------------------------------------------------------------

class RealtimeSubmission(models.Model):
    """
    Separate workflow for realtime feeds.

    Rules (business):
    - For GTFS / NeTEx: can be created only after static submission is published (stage 4).
    - For GBFS: can be created without static submission.
    - Admin confirms realtime separately; only then it is visible publicly.
    """

    PROTOCOL_GTFS_RT = 'gtfs_rt'
    PROTOCOL_SIRI = 'siri'
    PROTOCOL_GBFS = 'gbfs'
    PROTOCOL_CHOICES = [
        (PROTOCOL_GTFS_RT, 'GTFS-RT'),
        (PROTOCOL_SIRI, 'SIRI'),
        (PROTOCOL_GBFS, 'GBFS'),
    ]

    transport_organization = models.ForeignKey(
        'cases.TransportOrganization',
        related_name='realtime_submissions',
        on_delete=models.CASCADE,
    )
    submitted_by = models.ForeignKey(
        get_user_model(),
        related_name='realtime_submissions',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    protocol = models.CharField(max_length=10, choices=PROTOCOL_CHOICES)
    static_submission = models.ForeignKey(
        FeedSubmission,
        related_name='realtime_submissions',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        help_text='Published static submission this realtime depends on (required for GTFS-RT and SIRI).',
    )

    name = models.CharField(max_length=255, blank=True, null=True)
    note = models.TextField(max_length=2048, blank=True, null=True)
    license = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['transport_organization', '-created_at']),
            models.Index(fields=['protocol']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['static_submission', 'protocol'],
                name='unique_realtime_protocol_per_static_submission',
            )
        ]

    def allowed_endpoint_types(self) -> set:
        if self.protocol == self.PROTOCOL_GTFS_RT:
            return {'trip_update', 'vehicle_position', 'service_alert'}
        if self.protocol == self.PROTOCOL_SIRI:
            return {'sx', 'sm', 'vm', 'et', 'gm'}
        if self.protocol == self.PROTOCOL_GBFS:
            return {
                'gbfs', 'gbfs_versions', 'system_information', 'vehicle_types',
                'station_information', 'station_status', 'free_bike_status',
                'system_hours', 'system_alerts',
            }
        return set()

    def clean(self):
        super().clean()
        if self.protocol in {self.PROTOCOL_GTFS_RT, self.PROTOCOL_SIRI} and not self.static_submission_id:
            raise ValidationError({'static_submission': 'This field is required for GTFS-RT and SIRI.'})
        if self.protocol == self.PROTOCOL_GBFS and self.static_submission_id:
            raise ValidationError({'static_submission': 'GBFS must not be linked to a static submission.'})

    @property
    def current_stage(self) -> int:
        latest = self.history.order_by('-created_at').first()
        if latest is None:
            return 1
        return latest.stage_after

    @property
    def is_rejected(self) -> bool:
        latest = self.history.order_by('-created_at').first()
        return latest is not None and latest.event_type == RealtimeSubmissionHistory.EVENT_REJECTED

    @property
    def rejection_cause(self):
        if not self.is_rejected:
            return None
        latest = self.history.order_by('-created_at').first()
        return latest.cause if latest else None

    @property
    def published_at(self):
        completed = self.history.filter(
            event_type=RealtimeSubmissionHistory.EVENT_COMPLETED
        ).order_by('-created_at').first()
        return completed.created_at if completed else None

    def __str__(self):
        return f"RealtimeSubmission(id={self.id}, protocol={self.protocol}, org={self.transport_organization_id})"


class RealtimeSubmissionHistory(models.Model):
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
        RealtimeSubmission,
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
        related_name='realtime_history_actions',
    )
    cause = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return (
            f"RealtimeSubmissionHistory(submission={self.submission_id}, "
            f"event={self.event_type}, {self.stage_before}→{self.stage_after})"
        )


class RealtimeEndpointRT(models.Model):
    RT_ENDPOINT_TYPE_CHOICES = [
        ('trip_update', 'Trip Update (GTFS-RT)'),
        ('vehicle_position', 'Vehicle Position (GTFS-RT)'),
        ('service_alert', 'Service Alert (GTFS-RT)'),
        ('sx', 'Situation Exchange – SX (SIRI)'),
        ('sm', 'Stop Monitoring – SM (SIRI)'),
        ('vm', 'Vehicle Monitoring – VM (SIRI)'),
        ('et', 'Estimated Timetable – ET (SIRI)'),
        ('gm', 'General Message – GM (SIRI)'),
        ('gbfs', 'GBFS – Main file'),
        ('gbfs_versions', 'GBFS Versions'),
        ('system_information', 'System Information'),
        ('vehicle_types', 'Vehicle Types'),
        ('station_information', 'Station Information'),
        ('station_status', 'Station Status'),
        ('free_bike_status', 'Free Bike Status'),
        ('system_hours', 'System Hours'),
        ('system_alerts', 'System Alerts'),
    ]

    AUTH_TYPE_CHOICES = [
        ('api_key', 'API Key'),
        ('bearer_token', 'Bearer Token'),
        ('basic_auth', 'Basic Auth (user:pass)'),
    ]

    submission = models.ForeignKey(
        RealtimeSubmission,
        on_delete=models.CASCADE,
        related_name='endpoints',
    )
    endpoint_type = models.CharField(max_length=30, choices=RT_ENDPOINT_TYPE_CHOICES)
    url = models.URLField()
    hide_original = models.BooleanField(default=False)
    is_original = models.BooleanField(default=False)
    interval = models.PositiveIntegerField(help_text='Refresh interval in seconds.', default=60)
    auth_type = models.CharField(max_length=20, choices=AUTH_TYPE_CHOICES, null=True, blank=True)
    auth_value = models.CharField(max_length=512, blank=True, null=True)
    cached_file = models.FileField(
        upload_to=realtime_feed_cached_file_path,
        storage=OverwriteStorage(),
        blank=True,
        null=True,
        help_text='Server-cached copy when hide_original=True.',
    )
    cached_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('submission', 'endpoint_type')]
        ordering = ['submission', 'endpoint_type']

    def clean(self):
        super().clean()
        if self.auth_type:
            self.hide_original = True
        if self.submission_id:
            allowed = self.submission.allowed_endpoint_types()
            if self.endpoint_type not in allowed:
                raise ValidationError(
                    f"Endpoint type '{self.endpoint_type}' is not valid for "
                    f"protocol '{self.submission.protocol}'. Allowed: {sorted(allowed)}."
                )
        if self.hide_original and not self.auth_type:
            raise ValidationError('Authentication is required when "hide original" is enabled.')

    def __str__(self):
        return f"RealtimeEndpointRT(id={self.id}, type={self.endpoint_type}, submission={self.submission_id})"

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


def completed_realtime_submission_ids() -> list[int]:
    """PKs of RealtimeSubmission whose latest history row is stage 4 (published)."""
    from django.db.models import Subquery, OuterRef

    latest_history = RealtimeSubmissionHistory.objects.filter(
        submission=OuterRef('pk')
    ).order_by('-created_at')

    return list(
        RealtimeSubmission.objects.annotate(
            current_stage_val=Subquery(latest_history.values('stage_after')[:1])
        ).filter(
            current_stage_val=4
        ).values_list('id', flat=True)
    )