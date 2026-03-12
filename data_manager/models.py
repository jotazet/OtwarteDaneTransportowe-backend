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
    Returns: 'uploaded_data/{user_id}/{org_id}'
    Falls back to 'unknown' when IDs are not yet set (pre-save).
    """
    user_id = getattr(submission.submitted_by, 'id', None) or 'unknown'
    org_id = submission.transport_organization_id or 'unknown'
    return f'uploaded_data/{user_id}/{org_id}'


def static_feed_file_upload_to(instance, filename):
    """
    For files uploaded directly by the user.
    uploaded_data/{user_id}/{org_id}/static/{original_filename}
    """
    base = _build_base_path(instance.submission)
    return f'{base}/static/{filename}'


def static_feed_cached_upload_to(instance, filename):
    """
    For files downloaded automatically by the server (hide_original=True).
    uploaded_data/{user_id}/{org_id}/static/{original_filename}
    """
    base = _build_base_path(instance.submission)
    return f'{base}/static/{filename}'


def realtime_feed_cached_upload_to(instance, filename):
    """
    For realtime feed files downloaded automatically by the server (hide_original=True).
    uploaded_data/{user_id}/{org_id}/realtime/{endpoint_type}/{original_filename}
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
        ('gbfs', 'GBFS'),
        ('siri', 'SIRI'),
        # Dynamic
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
    feed_kind = models.CharField(max_length=10, choices=FEED_KIND_CHOICES)
    name = models.CharField(max_length=255, blank=True, null=True)
    note = models.TextField(max_length=2048, blank=True, null=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Progress stages (each set when the stage is reached)
    stage_upload_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Stage 1: Data upload date',
    )
    stage_verification_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Stage 2: Data verification date',
    )
    stage_confirmation_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Stage 3: Admin confirmation date',
    )
    stage_complete_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Stage 4: Upload complete date',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['transport_organization', '-created_at']),
            models.Index(fields=['data_type']),
            models.Index(fields=['feed_kind']),
        ]

    @property
    def current_stage(self) -> int:
        """Returns the current progress stage number (1–4, or 0 if not started)."""
        if self.stage_complete_at:
            return 4
        if self.stage_confirmation_at:
            return 3
        if self.stage_verification_at:
            return 2
        if self.stage_upload_at:
            return 1
        return 0

    @property
    def current_stage_label(self) -> str:
        labels = {
            0: 'Not started',
            1: 'Step 1: Data uploaded',
            2: 'Step 2: Data verification',
            3: 'Step 3: Admin confirmation',
            4: 'Step 4: Complete',
        }
        return labels[self.current_stage]

    def clean(self):
        super().clean()
        # Derive feed_kind from data_type automatically
        # gtfs_rt and siri are dynamic (real-time) feeds; all others are static
        if self.data_type in ('gtfs_rt', 'siri'):
            self.feed_kind = self.FEED_KIND_DYNAMIC
        else:
            self.feed_kind = self.FEED_KIND_STATIC

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        label = self.name or f"org_id={self.transport_organization_id}"
        return f"FeedSubmission(id={self.id}, {self.data_type}, {label})"


# ---------------------------------------------------------------------------
# StaticFeedEntry – for static feeds (GTFS, NeTEx, GBFS, SIRI, other)
# ---------------------------------------------------------------------------

class StaticFeedEntry(models.Model):
    AUTH_TYPE_CHOICES = [
        ('none', 'None'),
        ('api_key', 'API Key'),
        ('bearer_token', 'Bearer Token'),
        ('basic_auth', 'Basic Auth (user:pass)'),
    ]

    submission = models.OneToOneField(
        FeedSubmission,
        on_delete=models.CASCADE,
        related_name='static_entry',
    )

    license = models.CharField(max_length=255)

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
        has_cached_file = bool(self.cached_file)

        # User chooses exactly one input source: url XOR file.
        if has_url and has_file:
            raise ValidationError('Provide either a URL or a file upload, not both.')
        if not has_url and not has_file:
            raise ValidationError('Either a URL or a file upload must be provided.')

        # Only one stored file may exist in static/: manual upload OR server-downloaded copy.
        if has_file and has_cached_file:
            raise ValidationError(
                'Static feed can store either the uploaded file or the server-downloaded file, not both.'
            )

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

        # For URL submissions, cached_file is server-managed and is the only stored file copy when present.
        if has_url and has_cached_file and has_file:
            raise ValidationError(
                'For URL submissions, only the server-downloaded file should be stored.'
            )

    def __str__(self):
        source = self.url or getattr(self.file, 'name', '')
        return f"StaticFeedEntry(id={self.id}, submission={self.submission_id}, source='{source}')"


# ---------------------------------------------------------------------------
# RealtimeFeedEntry – shared container for GTFS-RT and SIRI
# ---------------------------------------------------------------------------

class RealtimeFeedEntry(models.Model):
    PROTOCOL_GTFS_RT = 'gtfs_rt'
    PROTOCOL_SIRI = 'siri'
    PROTOCOL_CHOICES = [
        (PROTOCOL_GTFS_RT, 'GTFS-RT'),
        (PROTOCOL_SIRI, 'SIRI'),
    ]

    # Endpoint types per protocol
    GTFS_RT_ENDPOINT_TYPES = {'trip_update', 'vehicle_position', 'service_alert'}
    SIRI_ENDPOINT_TYPES = {'sx', 'sm', 'vm', 'et', 'gm'}

    license = models.CharField(max_length=255)

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
        return self.SIRI_ENDPOINT_TYPES

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
    ENDPOINT_TRIP_UPDATE      = 'trip_update'
    ENDPOINT_VEHICLE_POSITION = 'vehicle_position'
    ENDPOINT_SERVICE_ALERT    = 'service_alert'
    # SIRI endpoint types
    ENDPOINT_SX = 'sx'
    ENDPOINT_SM = 'sm'
    ENDPOINT_VM = 'vm'
    ENDPOINT_ET = 'et'
    ENDPOINT_GM = 'gm'

    ENDPOINT_TYPE_CHOICES = [
        # GTFS-RT
        (ENDPOINT_TRIP_UPDATE,      'Trip Update (GTFS-RT)'),
        (ENDPOINT_VEHICLE_POSITION, 'Vehicle Position (GTFS-RT)'),
        (ENDPOINT_SERVICE_ALERT,    'Service Alert (GTFS-RT)'),
        # SIRI
        (ENDPOINT_SX, 'Situation Exchange – SX (SIRI)'),
        (ENDPOINT_SM, 'Stop Monitoring – SM (SIRI)'),
        (ENDPOINT_VM, 'Vehicle Monitoring – VM (SIRI)'),
        (ENDPOINT_ET, 'Estimated Timetable – ET (SIRI)'),
        (ENDPOINT_GM, 'General Message – GM (SIRI)'),
    ]

    entry = models.ForeignKey(
        RealtimeFeedEntry,
        on_delete=models.CASCADE,
        related_name='endpoints',
    )
    endpoint_type = models.CharField(max_length=20, choices=ENDPOINT_TYPE_CHOICES)
    url = models.URLField()
    hide_original = models.BooleanField(
        default=False,
        help_text='Server acts as a proxy and hides the original URL.',
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
