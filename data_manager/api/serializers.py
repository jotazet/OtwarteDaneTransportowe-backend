from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from rest_framework import serializers

from data_manager.models import (
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)
from cases.models import TransportOrganization
from data_manager.net_security import OutboundURLBlocked, assert_safe_outbound_url


# ---------------------------------------------------------------------------
# FeedSubmissionHistory
# ---------------------------------------------------------------------------

class FeedSubmissionHistorySerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    class Meta:
        model = FeedSubmissionHistory
        fields = [
            'id', 'event_type', 'stage_before', 'stage_after',
            'actor', 'cause', 'created_at',
        ]
        read_only_fields = fields

    def get_actor(self, obj):
        return obj.actor.username if obj.actor else None


class RealtimeSubmissionHistorySerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    class Meta:
        model = RealtimeSubmissionHistory
        fields = [
            'id', 'event_type', 'stage_before', 'stage_after',
            'actor', 'cause', 'created_at',
        ]
        read_only_fields = fields

    def get_actor(self, obj):
        return obj.actor.username if obj.actor else None


# ---------------------------------------------------------------------------
# Feed labelling helpers (organization name + "#<id> name" feed label)
# ---------------------------------------------------------------------------

def feed_organization_name(submission) -> str | None:
    """Human-readable organization name for a FeedSubmission/RealtimeSubmission."""
    if submission and submission.transport_organization_id:
        return submission.transport_organization.transport_organization
    return None


def feed_organization_region(submission) -> str | None:
    if submission and submission.transport_organization_id:
        return submission.transport_organization.region
    return None


def feed_display_name(submission) -> str | None:
    """Feed label combining its id and name, e.g. ``#23 ZTM Warszawa``."""
    if not submission:
        return None
    name = (submission.name or '').strip()
    return f"#{submission.id} {name}".strip()


def _fetch_health_fields(obj) -> dict:
    """Proxy fetch status + recent error aggregates for a static entry or RT endpoint."""
    since = timezone.now() - timedelta(days=7)
    last_error = obj.fetch_errors.order_by('-occurred_at').first()
    return {
        'fetch_status': obj.fetch_status,
        'fetch_failure_count': obj.fetch_failure_count,
        'next_fetch_after': obj.next_fetch_after,
        'fetch_paused_at': obj.fetch_paused_at,
        'fetch_pause_reason': obj.fetch_pause_reason or '',
        'last_fetch_success_at': obj.last_fetch_success_at,
        'last_fetch_error_at': obj.last_fetch_error_at,
        'last_fetch_error_message': last_error.message if last_error else None,
        'fetch_error_count_7d': obj.fetch_errors.filter(occurred_at__gte=since).count(),
    }


class ProxyManagedFeedListSerializer(serializers.Serializer):
    """Unified list item for static and realtime proxy-managed feeds."""

    source = serializers.CharField()
    id = serializers.IntegerField()
    submission_id = serializers.IntegerField()
    organization = serializers.CharField()
    region = serializers.CharField()
    feed_name = serializers.CharField()
    data_type = serializers.CharField(allow_null=True)
    protocol = serializers.CharField(allow_null=True)
    endpoint_type = serializers.CharField(allow_null=True)
    fetch_status = serializers.CharField()
    fetch_failure_count = serializers.IntegerField()
    next_fetch_after = serializers.DateTimeField(allow_null=True)
    fetch_paused_at = serializers.DateTimeField(allow_null=True)
    fetch_pause_reason = serializers.CharField()
    last_fetch_success_at = serializers.DateTimeField(allow_null=True)
    last_fetch_error_at = serializers.DateTimeField(allow_null=True)
    last_fetch_error_message = serializers.CharField(allow_null=True)
    fetch_error_count_7d = serializers.IntegerField()

    @staticmethod
    def from_static_entry(entry: StaticFeedEntry) -> dict:
        submission = entry.submission
        return {
            'source': 'static',
            'id': entry.id,
            'submission_id': submission.id,
            'organization': feed_organization_name(submission),
            'region': feed_organization_region(submission),
            'feed_name': feed_display_name(submission),
            'data_type': submission.data_type,
            'protocol': None,
            'endpoint_type': None,
            **_fetch_health_fields(entry),
        }

    @staticmethod
    def from_realtime_endpoint(endpoint: RealtimeEndpointRT) -> dict:
        submission = endpoint.submission
        return {
            'source': 'realtime',
            'id': endpoint.id,
            'submission_id': submission.id,
            'organization': feed_organization_name(submission),
            'region': feed_organization_region(submission),
            'feed_name': feed_display_name(submission),
            'data_type': None,
            'protocol': submission.protocol,
            'endpoint_type': endpoint.endpoint_type,
            **_fetch_health_fields(endpoint),
        }


# ---------------------------------------------------------------------------
# FeedFetchError
# ---------------------------------------------------------------------------

class FeedFetchErrorSerializer(serializers.ModelSerializer):
    source = serializers.SerializerMethodField()
    endpoint_type = serializers.SerializerMethodField()
    organization = serializers.SerializerMethodField()
    feed_name = serializers.SerializerMethodField()

    class Meta:
        model = FeedFetchError
        fields = [
            'id', 'source', 'static_entry', 'endpoint_rt', 'endpoint_type',
            'organization', 'feed_name',
            'error_type', 'http_status_code', 'message', 'url_attempted', 'occurred_at',
        ]
        read_only_fields = fields

    def get_source(self, obj):
        return 'static' if obj.static_entry_id else 'realtime'

    def get_endpoint_type(self, obj):
        return obj.endpoint_rt.endpoint_type if obj.endpoint_rt else None

    def _submission(self, obj):
        if obj.static_entry_id:
            return obj.static_entry.submission
        if obj.endpoint_rt_id:
            return obj.endpoint_rt.submission
        return None

    def get_organization(self, obj):
        return feed_organization_name(self._submission(obj))

    def get_feed_name(self, obj):
        return feed_display_name(self._submission(obj))


class FetchHealthSerializerMixin(serializers.Serializer):
    last_fetch_error_message = serializers.SerializerMethodField()
    fetch_error_count_7d = serializers.SerializerMethodField()

    def get_last_fetch_error_message(self, obj):
        error = obj.fetch_errors.order_by('-occurred_at').first()
        return error.message if error else None

    def get_fetch_error_count_7d(self, obj):
        since = timezone.now() - timedelta(days=7)
        return obj.fetch_errors.filter(occurred_at__gte=since).count()


# ---------------------------------------------------------------------------
# StaticFeedEntry – private (owner)
# ---------------------------------------------------------------------------

STATIC_ENTRY_SOURCE_FIELDS = frozenset({
    'url', 'file', 'is_original', 'hide_original', 'auth_type', 'auth_value',
})

PROXY_FETCH_FIELDS = frozenset({
    'fetch_status', 'fetch_failure_count', 'next_fetch_after',
    'fetch_paused_at', 'fetch_pause_reason',
    'last_fetch_success_at', 'last_fetch_error_at',
    'last_fetch_error_message', 'fetch_error_count_7d',
})


class StaticFeedEntrySerializer(FetchHealthSerializerMixin, serializers.ModelSerializer):
    organization = serializers.SerializerMethodField()
    region = serializers.SerializerMethodField()
    feed_name = serializers.SerializerMethodField()
    is_proxy_managed = serializers.BooleanField(read_only=True)

    class Meta:
        model = StaticFeedEntry
        fields = [
            'id', 'organization', 'region', 'feed_name', 'is_proxy_managed',
            'url', 'file', 'is_original', 'hide_original',
            'auth_type', 'auth_value',
            'download_time_1', 'download_time_2',
            'license', 'cached_at', 'uploaded_at',
            'validation_status', 'validation_message',
            'fetch_status', 'fetch_failure_count', 'next_fetch_after',
            'fetch_paused_at', 'fetch_pause_reason',
            'last_fetch_success_at', 'last_fetch_error_at',
            'last_fetch_error_message', 'fetch_error_count_7d',
        ]
        read_only_fields = [
            'id', 'organization', 'region', 'feed_name', 'is_proxy_managed', 'cached_at', 'uploaded_at',
            'validation_status', 'validation_message',
            'fetch_status', 'fetch_failure_count', 'next_fetch_after',
            'fetch_paused_at', 'fetch_pause_reason',
            'last_fetch_success_at', 'last_fetch_error_at',
            'last_fetch_error_message', 'fetch_error_count_7d',
        ]
        extra_kwargs = {
            'auth_value': {'write_only': True},
            'file': {'required': False},
            'url': {'required': False},
            'auth_type': {'required': False, 'allow_null': True},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.context.get('restricted_static_edit'):
            for field_name in STATIC_ENTRY_SOURCE_FIELDS:
                field = self.fields.get(field_name)
                if field is not None:
                    field.read_only = True

    def get_organization(self, obj):
        return feed_organization_name(obj.submission)

    def get_region(self, obj):
        return feed_organization_region(obj.submission)

    def get_feed_name(self, obj):
        return feed_display_name(obj.submission)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not instance.is_proxy_managed:
            for field_name in PROXY_FETCH_FIELDS:
                data.pop(field_name, None)
        return data

    def validate_url(self, value: str | None):
        if not value:
            return value
        try:
            assert_safe_outbound_url(value)
        except OutboundURLBlocked as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

# ---------------------------------------------------------------------------
# RealtimeSubmission – realtime flow
# ---------------------------------------------------------------------------

class RealtimeEndpointRTSerializer(FetchHealthSerializerMixin, serializers.ModelSerializer):
    organization = serializers.SerializerMethodField()
    region = serializers.SerializerMethodField()
    feed_name = serializers.SerializerMethodField()
    is_proxy_managed = serializers.BooleanField(read_only=True)

    class Meta:
        model = RealtimeEndpointRT
        fields = [
            'id', 'organization', 'region', 'feed_name', 'is_proxy_managed',
            'endpoint_type', 'url', 'is_original',
            'hide_original', 'auth_type', 'auth_value',
            'interval', 'cached_at',
            'fetch_status', 'fetch_failure_count', 'next_fetch_after',
            'fetch_paused_at', 'fetch_pause_reason',
            'last_fetch_success_at', 'last_fetch_error_at',
            'last_fetch_error_message', 'fetch_error_count_7d',
        ]
        read_only_fields = [
            'id', 'organization', 'region', 'feed_name', 'is_proxy_managed', 'cached_at',
            'fetch_status', 'fetch_failure_count', 'next_fetch_after',
            'fetch_paused_at', 'fetch_pause_reason',
            'last_fetch_success_at', 'last_fetch_error_at',
            'last_fetch_error_message', 'fetch_error_count_7d',
        ]
        extra_kwargs = {'auth_value': {'write_only': True}}

    def get_organization(self, obj):
        return feed_organization_name(obj.submission)

    def get_region(self, obj):
        return feed_organization_region(obj.submission)

    def get_feed_name(self, obj):
        return feed_display_name(obj.submission)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not instance.is_proxy_managed:
            for field_name in PROXY_FETCH_FIELDS:
                data.pop(field_name, None)
        return data


class RealtimeEndpointRTWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = RealtimeEndpointRT
        fields = [
            'endpoint_type', 'url', 'is_original',
            'hide_original', 'auth_type', 'auth_value', 'interval',
        ]
        extra_kwargs = {
            'auth_value': {'write_only': True, 'required': False},
            'auth_type': {'required': False, 'allow_null': True},
        }

    def validate_url(self, value: str):
        try:
            assert_safe_outbound_url(value)
        except OutboundURLBlocked as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class RealtimeSubmissionSerializer(serializers.ModelSerializer):
    endpoints = RealtimeEndpointRTSerializer(many=True, read_only=True)
    current_stage = serializers.IntegerField(read_only=True)
    current_stage_label = serializers.SerializerMethodField()
    is_rejected = serializers.BooleanField(read_only=True)
    rejection_cause = serializers.CharField(read_only=True, allow_null=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)
    history = RealtimeSubmissionHistorySerializer(many=True, read_only=True)
    static_submission = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = RealtimeSubmission
        fields = [
            'id', 'transport_organization', 'submitted_by', 'static_submission',
            'protocol', 'name', 'note', 'license',
            'created_at', 'updated_at',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'endpoints', 'history',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'submitted_by',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'history',
        ]

    def get_current_stage_label(self, obj):
        if obj.is_rejected:
            return 'Rejected'
        labels = {
            1: 'Step 1: Endpoints',
            2: 'Step 2: Data verification',
            3: 'Step 3: Admin confirmation',
            4: 'Step 4: Published',
        }
        return labels.get(obj.current_stage, 'Unknown')


class RealtimeSubmissionWriteSerializer(serializers.ModelSerializer):
    endpoints = RealtimeEndpointRTWriteSerializer(many=True, required=False)
    transport_organization = serializers.PrimaryKeyRelatedField(
        queryset=TransportOrganization.objects.all(),
    )
    static_submission = serializers.PrimaryKeyRelatedField(
        queryset=FeedSubmission.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = RealtimeSubmission
        fields = [
            'transport_organization', 'static_submission', 'protocol',
            'name', 'note', 'license', 'endpoints',
        ]

    def to_internal_value(self, data):
        if self.context.get('restricted_realtime_edit') and self.instance and isinstance(data, dict):
            eps = data.get('endpoints')
            if isinstance(eps, list):
                by_type = {e.endpoint_type: e for e in self.instance.endpoints.all()}
                merged = []
                for item in eps:
                    if isinstance(item, dict) and item.get('endpoint_type') in by_type:
                        ex = by_type[item['endpoint_type']]
                        row = dict(item)
                        row.setdefault('url', ex.url)
                        row.setdefault('hide_original', ex.hide_original)
                        row.setdefault('is_original', ex.is_original)
                        row.setdefault('auth_type', ex.auth_type)
                        row.setdefault('auth_value', ex.auth_value)
                        merged.append(row)
                    else:
                        merged.append(item)
                data = {**data, 'endpoints': merged}
        return super().to_internal_value(data)

    def validate(self, data):
        from data_manager.models import completed_submission_ids

        inst = getattr(self, 'instance', None)
        restricted = self.context.get('restricted_realtime_edit', False)
        if restricted and inst is not None:
            if 'name' in data:
                old_n = inst.name or ''
                new_n = data.get('name') or ''
                if old_n != new_n:
                    raise serializers.ValidationError({
                        'name': 'Cannot change name after stage 1.',
                    })
            if 'transport_organization' in data:
                if data['transport_organization'].pk != inst.transport_organization_id:
                    raise serializers.ValidationError({
                        'transport_organization': (
                            'Cannot change this field after stage 1.'
                        ),
                    })
            if 'static_submission' in data:
                new_ss = data['static_submission']
                new_id = new_ss.pk if new_ss else None
                if new_id != inst.static_submission_id:
                    raise serializers.ValidationError({
                        'static_submission': (
                            'Cannot change this field after stage 1.'
                        ),
                    })
            if 'protocol' in data and data['protocol'] != inst.protocol:
                raise serializers.ValidationError({
                    'protocol': 'Cannot change this field after stage 1.',
                })

        protocol = data.get('protocol', inst.protocol if inst else '')
        org = data.get('transport_organization', inst.transport_organization if inst else None)
        ss = data.get('static_submission', inst.static_submission if inst else None)
        endpoints = data.get('endpoints')
        if endpoints is not None and not endpoints:
            raise serializers.ValidationError({'endpoints': 'At least one endpoint is required.'})
        if endpoints is None and inst is None:
            raise serializers.ValidationError({'endpoints': 'At least one endpoint is required.'})

        if restricted and inst is not None and endpoints is not None:
            existing_by = {e.endpoint_type: e for e in inst.endpoints.all()}
            if len(endpoints) != len(existing_by):
                raise serializers.ValidationError({
                    'endpoints': 'Cannot add or remove endpoints after validation.',
                })
            for ep in endpoints:
                t = ep['endpoint_type']
                ex = existing_by.get(t)
                if ex is None:
                    raise serializers.ValidationError({
                        'endpoints': f"Unknown endpoint type '{t}'.",
                    })
                for field in ('url', 'auth_type', 'hide_original', 'is_original'):
                    if ep.get(field) != getattr(ex, field):
                        raise serializers.ValidationError({
                            'endpoints': f"Cannot change {field!r} after stage 1.",
                        })
                av_ep = ep.get('auth_value') or None
                av_ex = ex.auth_value or None
                if av_ep != av_ex:
                    raise serializers.ValidationError({
                        'endpoints': 'Cannot change auth credentials after validation.',
                    })

        if protocol == RealtimeSubmission.PROTOCOL_GBFS:
            if ss is not None:
                raise serializers.ValidationError({'static_submission': 'GBFS must not reference a static submission.'})
        elif protocol in (RealtimeSubmission.PROTOCOL_GTFS_RT, RealtimeSubmission.PROTOCOL_SIRI):
            if ss is None:
                raise serializers.ValidationError({'static_submission': 'This field is required for GTFS-RT and SIRI.'})
            if org and ss.transport_organization_id != org.id:
                raise serializers.ValidationError(
                    {'static_submission': 'Static submission must belong to the same organization.'}
                )
            allowed_protocols = RealtimeSubmission.allowed_protocols_for_static_data_type(ss.data_type)
            if protocol not in allowed_protocols:
                raise serializers.ValidationError(
                    {
                        'static_submission': (
                            f"Protocol '{protocol}' cannot be linked to static feed type "
                            f"'{ss.data_type}'. Allowed: {sorted(allowed_protocols)}."
                        )
                    }
                )
            if ss.id not in completed_submission_ids():
                raise serializers.ValidationError(
                    {'static_submission': 'Static feed must be published (completed) before adding realtime.'}
                )
        tmp = RealtimeSubmission(protocol=protocol)
        allowed = tmp.allowed_endpoint_types()
        if endpoints is not None:
            seen = set()
            for ep in endpoints:
                t = ep.get('endpoint_type', '')
                if t not in allowed:
                    raise serializers.ValidationError(
                        {'endpoints': f"Invalid endpoint_type '{t}' for protocol '{protocol}'. Allowed: {sorted(allowed)}."}
                    )
                if t in seen:
                    raise serializers.ValidationError({'endpoints': f"Duplicate endpoint_type '{t}'."})
                seen.add(t)

        return data


class EligibleRealtimeStaticSubmissionSerializer(serializers.ModelSerializer):
    current_stage = serializers.IntegerField(read_only=True)
    current_stage_label = serializers.CharField(read_only=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)
    allowed_realtime_protocols = serializers.SerializerMethodField()

    class Meta:
        model = FeedSubmission
        fields = [
            'id',
            'transport_organization',
            'submitted_by',
            'data_type',
            'name',
            'current_stage',
            'current_stage_label',
            'published_at',
            'allowed_realtime_protocols',
        ]
        read_only_fields = fields

    def get_allowed_realtime_protocols(self, obj):
        existing_protocols = {rt.protocol for rt in obj.realtime_submissions.all()}
        allowed = RealtimeSubmission.allowed_protocols_for_static_data_type(obj.data_type)
        return sorted(allowed - existing_protocols)


# ---------------------------------------------------------------------------
# FeedSubmission – private list (owner)
# ---------------------------------------------------------------------------

class FeedSubmissionListSerializer(serializers.ModelSerializer):
    current_stage = serializers.IntegerField(read_only=True)
    current_stage_label = serializers.CharField(read_only=True)
    is_rejected = serializers.BooleanField(read_only=True)
    has_rejection_cause = serializers.SerializerMethodField()

    class Meta:
        model = FeedSubmission
        fields = [
            'id', 'transport_organization', 'data_type',
            'current_stage', 'current_stage_label',
            'is_rejected', 'published_at',
            'created_at', 'updated_at', 'has_rejection_cause'
        ]
        read_only_fields = fields

    def get_has_rejection_cause(self, obj: FeedSubmission) -> bool:
        return bool(obj.rejection_cause)


class UserFeedSubmissionListSerializer(FeedSubmissionListSerializer):
    transport_organization = serializers.CharField(
        source='transport_organization.transport_organization',
        read_only=True,
    )


# ---------------------------------------------------------------------------
# FeedSubmission – detail (owner/admin)
# ---------------------------------------------------------------------------

class FeedSubmissionSerializer(serializers.ModelSerializer):
    static_entry = StaticFeedEntrySerializer(read_only=True)
    realtime_submissions = RealtimeSubmissionSerializer(many=True, read_only=True)
    current_stage = serializers.IntegerField(read_only=True)
    current_stage_label = serializers.CharField(read_only=True)
    is_rejected = serializers.BooleanField(read_only=True)
    rejection_cause = serializers.CharField(read_only=True, allow_null=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)
    history = FeedSubmissionHistorySerializer(many=True, read_only=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'id', 'transport_organization',
            'submitted_by',
            'data_type', 'name', 'note',
            'created_at', 'updated_at',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'static_entry', 'realtime_submissions',
            'history',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at',
            'submitted_by',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'history',
        ]


# ---------------------------------------------------------------------------
# FeedSubmission – write (create / update)
# ---------------------------------------------------------------------------

def _static_entry_keys_in_request(data) -> set[str]:
    """Field names sent under static_entry (JSON body or multipart keys)."""
    if not isinstance(data, dict):
        return set()
    nested = data.get('static_entry')
    if isinstance(nested, dict):
        return {str(k) for k in nested.keys()}
    keys: set[str] = set()
    prefix = 'static_entry.'
    for key in data:
        if isinstance(key, str) and key.startswith(prefix):
            keys.add(key[len(prefix):].split('.')[0])
    return keys


def _prune_noop_static_entry_source_updates(
    entry: StaticFeedEntry, data: dict, partial: bool
) -> dict:
    """Avoid clearing url/file on PATCH when the client resends empty placeholders."""
    if not partial:
        return dict(data)

    def clearing(v):
        return v is None or v == ''

    out = dict(data)
    url_in = 'url' in out
    file_in = 'file' in out
    new_url = url_in and not clearing(out['url'])
    new_file = file_in and not clearing(out['file'])

    if url_in and clearing(out['url']) and not new_file:
        out.pop('url', None)
    if file_in and clearing(out['file']) and not new_url:
        out.pop('file', None)
    return out


class FeedSubmissionWriteSerializer(serializers.ModelSerializer):
    static_entry = StaticFeedEntrySerializer(required=False, allow_null=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'transport_organization', 'data_type', 'name', 'note',
            'static_entry',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Nested serializer is re-bound per request so restricted_static_edit applies read_only.
        self.fields['static_entry'] = StaticFeedEntrySerializer(
            required=False,
            allow_null=True,
            context=self.context,
        )

    def validate(self, attrs):
        restricted = self.context.get('restricted_static_edit', False)
        if restricted:
            if 'name' in attrs:
                old_n = (self.instance.name or '') if self.instance else ''
                new_n = attrs.get('name') or ''
                if old_n != new_n:
                    raise serializers.ValidationError({
                        'name': 'Cannot change name after stage 1.',
                    })
            if 'transport_organization' in attrs:
                raise serializers.ValidationError({
                    'transport_organization': (
                        'Cannot change organization after stage 1.'
                    ),
                })
            if 'data_type' in attrs:
                raise serializers.ValidationError({
                    'data_type': (
                        'Cannot change data type after stage 1.'
                    ),
                })
            allowed = {'license', 'download_time_1', 'download_time_2'}
            sent_keys = _static_entry_keys_in_request(self.initial_data)
            bad_sent = sent_keys - allowed
            if bad_sent:
                raise serializers.ValidationError({
                    'static_entry': (
                        'After stage 1, only license and download schedule times (UTC) can be changed '
                        f'(disallowed: {", ".join(sorted(bad_sent))}).'
                    ),
                })
            static_entry = attrs.get('static_entry')
            if static_entry is not None:
                bad = set(static_entry.keys()) - allowed
                if bad:
                    raise serializers.ValidationError({
                        'static_entry': (
                            'After stage 1, only license and download schedule times (UTC) can be changed '
                            f'(disallowed: {", ".join(sorted(bad))}).'
                        ),
                    })
                if self.instance is not None:
                    try:
                        entry = self.instance.static_entry
                    except ObjectDoesNotExist:
                        entry = None
                    if entry is not None:
                        for field_name in STATIC_ENTRY_SOURCE_FIELDS:
                            if field_name not in static_entry:
                                continue
                            new_val = static_entry[field_name]
                            old_val = getattr(entry, field_name)
                            if field_name in ('file',) and new_val:
                                old_name = getattr(old_val, 'name', None) if old_val else None
                                new_name = getattr(new_val, 'name', None)
                                if new_name != old_name:
                                    raise serializers.ValidationError({
                                        'static_entry': {
                                            field_name: 'Cannot change this field after stage 1.',
                                        },
                                    })
                            elif new_val != old_val:
                                raise serializers.ValidationError({
                                    'static_entry': {
                                        field_name: 'Cannot change this field after stage 1.',
                                    },
                                })

        static_entry = attrs.get('static_entry')
        if not static_entry:
            return attrs

        if self.instance is None:
            if not static_entry.get('url') and not static_entry.get('file'):
                raise serializers.ValidationError({
                    'static_entry': 'Provide a URL or file for the static feed entry.',
                })
            return attrs

        try:
            entry = self.instance.static_entry
        except ObjectDoesNotExist:
            entry = None

        if entry is None:
            if not static_entry.get('url') and not static_entry.get('file'):
                raise serializers.ValidationError({
                    'static_entry': (
                        'Provide a URL or file when adding a static feed entry '
                        'to this submission.'
                    ),
                })
            return attrs

        return attrs

    def create(self, validated_data):
        static_entry_data = validated_data.pop('static_entry', None)
        submission = FeedSubmission(**validated_data)
        submission.save()

        if static_entry_data:
            if static_entry_data.get('auth_type') is not None:
                static_entry_data['hide_original'] = True
            entry = StaticFeedEntry(submission=submission, **static_entry_data)
            entry.full_clean()
            entry.save()
            if entry.url:
                if entry.is_proxy_managed:
                    from data_manager.tasks import fetch_static_entry_task
                    fetch_static_entry_task.delay(entry.id)
                elif entry.submission.data_type == 'gtfs':
                    from data_manager.tasks import validate_gtfs_feed_task
                    validate_gtfs_feed_task.delay(entry.id)

        return submission

    @transaction.atomic
    def update(self, instance, validated_data):
        static_entry_data = validated_data.pop('static_entry', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if static_entry_data:
            try:
                entry = instance.static_entry
            except ObjectDoesNotExist:
                entry = None

            if entry is not None:
                static_entry_data = _prune_noop_static_entry_source_updates(
                    entry, static_entry_data, self.partial
                )

            if static_entry_data:
                if entry is not None:
                    for attr, value in static_entry_data.items():
                        setattr(entry, attr, value)
                    if static_entry_data.get('auth_type') is not None:
                        entry.hide_original = True
                    entry.full_clean()
                    entry.save()
                else:
                    if static_entry_data.get('auth_type') is not None:
                        static_entry_data['hide_original'] = True
                    new_entry = StaticFeedEntry(
                        submission=instance, **static_entry_data
                    )
                    new_entry.full_clean()
                    new_entry.save()

        return instance


# ---------------------------------------------------------------------------
# Admin FeedSubmission serializer – sees everything
# ---------------------------------------------------------------------------

class AdminFeedSubmissionSerializer(FeedSubmissionSerializer):
    submitted_by_username = serializers.SerializerMethodField()

    class Meta(FeedSubmissionSerializer.Meta):
        fields = FeedSubmissionSerializer.Meta.fields + ['submitted_by_username', 'note']
        read_only_fields = FeedSubmissionSerializer.Meta.read_only_fields + ['submitted_by_username']

    def get_submitted_by_username(self, obj):
        return obj.submitted_by.username if obj.submitted_by else None


# ---------------------------------------------------------------------------
# PUBLIC serializers
# ---------------------------------------------------------------------------

class PublishedStaticEntrySerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = StaticFeedEntry
        fields = ['download_url', 'license', 'cached_at', 'is_original']

    def get_download_url(self, obj):
        request = self.context.get('request')
        if obj.hide_original or obj.file:
            feed_file = obj.cached_file or obj.file
            if feed_file:
                if request:
                    filename = feed_file.name.split('/')[-1]
                    return request.build_absolute_uri(
                        f'/feed/{obj.submission_id}/{filename}'
                    )
            return None
        return obj.url


class PublishedEndpointRTSerializer(serializers.ModelSerializer):
    feed_url = serializers.SerializerMethodField()

    class Meta:
        model = RealtimeEndpointRT
        fields = ['endpoint_type', 'interval', 'feed_url', 'cached_at', 'is_original']

    def get_feed_url(self, obj):
        request = self.context.get('request')
        if obj.hide_original:
            if obj.cached_file:
                if request:
                    filename = obj.cached_file.name.split('/')[-1]
                    return request.build_absolute_uri(
                        f'/feed/rt/{obj.submission_id}/{filename}'
                    )
            return None
        return obj.url


class PublishedRealtimeSubmissionSerializer(serializers.ModelSerializer):
    endpoints = PublishedEndpointRTSerializer(many=True, read_only=True)

    class Meta:
        model = RealtimeSubmission
        fields = ['protocol', 'license', 'published_at', 'endpoints']


class PublishedFeedSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(
        source='transport_organization.transport_organization', read_only=True
    )
    organization_region = serializers.CharField(
        source='transport_organization.region', read_only=True
    )
    static_feed = PublishedStaticEntrySerializer(source='static_entry', read_only=True)
    realtime_feed = serializers.SerializerMethodField()
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'id',
            'organization_name',
            'organization_region',
            'data_type',
            'created_at',
            'published_at',
            'static_feed',
            'realtime_feed',
        ]
        read_only_fields = fields

    def get_realtime_feed(self, obj):
        rts = self.context.get('rt_embed', {}).get(obj.id)
        if not rts:
            return None
        return PublishedRealtimeSubmissionSerializer(rts, context=self.context).data


class PublishedGbfsFeedSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(
        source='transport_organization.transport_organization', read_only=True
    )
    organization_region = serializers.CharField(
        source='transport_organization.region', read_only=True
    )
    data_type = serializers.SerializerMethodField()
    endpoints = PublishedEndpointRTSerializer(many=True, read_only=True)

    class Meta:
        model = RealtimeSubmission
        fields = [
            'id',
            'organization_name',
            'organization_region',
            'data_type',
            'created_at',
            'published_at',
            'protocol',
            'license',
            'endpoints',
        ]
        read_only_fields = fields

    def get_data_type(self, obj):
        return 'gbfs'


class FeedListSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(
        source='transport_organization.transport_organization', read_only=True
    )
    organization_region = serializers.CharField(
        source='transport_organization.region', read_only=True
    )
    static_summary = serializers.SerializerMethodField()
    realtime_endpoint_types = serializers.SerializerMethodField()

    class Meta:
        model = FeedSubmission
        fields = [
            'id',
            'organization_name',
            'organization_region',
            'data_type',
            'name',
            'created_at',
            'static_summary',
            'realtime_endpoint_types',
        ]
        read_only_fields = fields

    def get_static_summary(self, obj):
        entry = getattr(obj, 'static_entry', None)
        if not entry:
            return {'has_static': False, 'count': 0, 'sources': []}
        source = 'url' if entry.url else 'file'
        return {'has_static': True, 'count': 1, 'sources': [source]}

    def get_realtime_endpoint_types(self, obj):
        rts = self.context.get('rt_embed', {}).get(obj.id)
        if not rts:
            return []
        return [ep.endpoint_type for ep in rts.endpoints.all()]


class FeedDetailSerializer(FeedSubmissionSerializer):
    class Meta(FeedSubmissionSerializer.Meta):
        fields = [
            'id', 'transport_organization',
            'submitted_by',
            'data_type', 'name', 'note',
            'created_at', 'updated_at',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'static_entry', 'realtime_submissions',
        ]
        read_only_fields = FeedSubmissionSerializer.Meta.read_only_fields


# ---------------------------------------------------------------------------
# Organization-level feed serializers
# ---------------------------------------------------------------------------

class OrganizationFeedSubmissionSerializer(serializers.ModelSerializer):
    submitted_by = serializers.SerializerMethodField()
    static_feed = PublishedStaticEntrySerializer(source='static_entry', read_only=True)
    realtime_feed = serializers.SerializerMethodField()

    class Meta:
        model = FeedSubmission
        fields = [
            'id', 'name', 'data_type',
            'submitted_by', 'created_at', 'updated_at',
            'static_feed', 'realtime_feed',
        ]
        read_only_fields = fields

    def get_submitted_by(self, obj):
        return obj.submitted_by.username if obj.submitted_by else None

    def get_realtime_feed(self, obj):
        rts = self.context.get('rt_embed', {}).get(obj.id)
        if not rts:
            return None
        return PublishedRealtimeSubmissionSerializer(rts, context=self.context).data


class OrganizationGbfsFeedAsFeedSerializer(serializers.ModelSerializer):
    data_type = serializers.SerializerMethodField()
    submitted_by = serializers.SerializerMethodField()
    static_feed = serializers.SerializerMethodField()
    realtime_feed = serializers.SerializerMethodField()

    class Meta:
        model = RealtimeSubmission
        fields = [
            'id', 'name', 'data_type',
            'submitted_by', 'created_at', 'updated_at',
            'static_feed', 'realtime_feed',
        ]
        read_only_fields = fields

    def get_data_type(self, obj):
        return 'gbfs'

    def get_submitted_by(self, obj):
        return obj.submitted_by.username if getattr(obj, 'submitted_by', None) else None

    def get_static_feed(self, obj):
        return None

    def get_realtime_feed(self, obj):
        return PublishedRealtimeSubmissionSerializer(obj, context=self.context).data


class OrganizationFeedsSerializer(serializers.ModelSerializer):
    feeds = serializers.SerializerMethodField()

    class Meta:
        model = TransportOrganization
        fields = [
            'id', 'region', 'transport_organization', 'website', 'contact_email', 'phone_number', 'is_public',
            'feeds',
        ]
        read_only_fields = fields

    def get_feeds(self, obj):
        static_feeds = getattr(obj, 'feeds', []) or []
        gbfs_feeds = getattr(obj, 'published_gbfs_feeds', None) or []

        static_payload = OrganizationFeedSubmissionSerializer(
            static_feeds, many=True, context=self.context
        ).data
        gbfs_payload = OrganizationGbfsFeedAsFeedSerializer(
            gbfs_feeds, many=True, context=self.context
        ).data

        combined = list(static_payload) + list(gbfs_payload)
        combined.sort(key=lambda x: (x.get('created_at') or '', x.get('id') or 0))
        return combined


class OrganizationFeedsSummarySerializer(serializers.ModelSerializer):
    static_types = serializers.SerializerMethodField()
    dynamic_types = serializers.SerializerMethodField()

    class Meta:
        model = TransportOrganization
        fields = [
            'id', 'region', 'transport_organization', 'website', 'contact_email', 'phone_number', 'is_public',
            'static_types', 'dynamic_types',
        ]
        read_only_fields = fields

    def get_static_types(self, obj):
        feeds = getattr(obj, 'feeds', []) or []
        return sorted({f.data_type for f in feeds})

    def get_dynamic_types(self, obj):
        types: set[str] = set()

        # GBFS is stored as published realtime submissions without static_submission.
        gbfs = getattr(obj, 'published_gbfs_feeds', None) or []
        if gbfs:
            types.add('gbfs')

        # GTFS-RT / SIRI are published realtime submissions attached to published static submissions.
        # Those are embedded into serializer context as {static_submission_id: RealtimeSubmission}.
        rt_embed = (self.context or {}).get('rt_embed', {}) or {}
        static_feeds = getattr(obj, 'feeds', []) or []
        for f in static_feeds:
            rts = rt_embed.get(getattr(f, 'id', None))
            if not rts:
                continue
            if rts.protocol == 'gtfs_rt':
                types.add('gtfs-rt')
            elif rts.protocol == 'siri':
                types.add('siri')

        return sorted(types)


class AdminRealtimeSubmissionSerializer(RealtimeSubmissionSerializer):
    submitted_by_username = serializers.SerializerMethodField()

    class Meta(RealtimeSubmissionSerializer.Meta):
        fields = RealtimeSubmissionSerializer.Meta.fields + ['submitted_by_username']
        read_only_fields = RealtimeSubmissionSerializer.Meta.read_only_fields + ['submitted_by_username']

    def get_submitted_by_username(self, obj):
        return obj.submitted_by.username if obj.submitted_by else None


class UserRealtimeSubmissionSerializer(serializers.ModelSerializer):
    transport_organization = serializers.CharField(
        source='transport_organization.transport_organization',
        read_only=True,
    )
    current_stage = serializers.IntegerField(read_only=True)
    current_stage_label = serializers.SerializerMethodField()
    is_rejected = serializers.BooleanField(read_only=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)
    has_rejection_cause = serializers.SerializerMethodField()

    class Meta:
        model = RealtimeSubmission
        fields = [
            'id',
            'transport_organization',
            'protocol',
            'current_stage',
            'current_stage_label',
            'is_rejected',
            'published_at',
            'created_at',
            'updated_at',
            'has_rejection_cause',
        ]
        read_only_fields = fields

    def get_current_stage_label(self, obj):
        if obj.is_rejected:
            return 'Rejected'
        labels = {
            1: 'Step 1: Endpoints',
            2: 'Step 2: Data verification',
            3: 'Step 3: Admin confirmation',
            4: 'Step 4: Published',
        }
        return labels.get(obj.current_stage, 'Unknown')

    def get_has_rejection_cause(self, obj: RealtimeSubmission) -> bool:
        return bool(obj.rejection_cause)
