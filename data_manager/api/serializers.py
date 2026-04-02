from django.db import transaction
from rest_framework import serializers

from data_manager.models import (
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpoint,
    RealtimeFeedEntry,
    StaticFeedEntry,
)
from cases.models import TransportOrganization


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


# ---------------------------------------------------------------------------
# FeedFetchError
# ---------------------------------------------------------------------------

class FeedFetchErrorSerializer(serializers.ModelSerializer):
    source = serializers.SerializerMethodField()
    endpoint_type = serializers.SerializerMethodField()

    class Meta:
        model = FeedFetchError
        fields = [
            'id', 'source', 'static_entry', 'endpoint', 'endpoint_type',
            'error_type', 'http_status_code', 'message', 'url_attempted', 'occurred_at',
        ]
        read_only_fields = fields

    def get_source(self, obj):
        return 'static' if obj.static_entry_id else 'realtime'

    def get_endpoint_type(self, obj):
        return obj.endpoint.endpoint_type if obj.endpoint else None


# ---------------------------------------------------------------------------
# StaticFeedEntry – private (owner)
# ---------------------------------------------------------------------------

class StaticFeedEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StaticFeedEntry
        fields = [
            'id', 'url', 'file', 'is_original', 'hide_original',
            'auth_type', 'auth_value',
            'download_time_1', 'download_time_2',
            'license', 'cached_at', 'uploaded_at',
        ]
        read_only_fields = ['id', 'cached_at', 'uploaded_at']
        extra_kwargs = {
            'auth_value': {'write_only': True},
            'file': {'required': False},
            'url': {'required': False},
        }


# ---------------------------------------------------------------------------
# RealtimeEndpoint / RealtimeFeedEntry – private
# ---------------------------------------------------------------------------

class RealtimeEndpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = RealtimeEndpoint
        fields = [
            'id', 'endpoint_type', 'url', 'is_original',
            'hide_original', 'auth_type', 'auth_value',
            'interval', 'cached_at',
        ]
        read_only_fields = ['id', 'cached_at']
        extra_kwargs = {'auth_value': {'write_only': True}}


class RealtimeFeedEntrySerializer(serializers.ModelSerializer):
    endpoints = RealtimeEndpointSerializer(many=True, read_only=True)

    class Meta:
        model = RealtimeFeedEntry
        fields = ['id', 'protocol', 'license', 'uploaded_at', 'endpoints']
        read_only_fields = ['id', 'uploaded_at']


class RealtimeEndpointWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = RealtimeEndpoint
        fields = [
            'endpoint_type', 'url', 'is_original',
            'hide_original', 'auth_type', 'auth_value', 'interval',
        ]
        extra_kwargs = {'auth_value': {'write_only': True, 'required': False}}


class RealtimeFeedEntryWriteSerializer(serializers.ModelSerializer):
    endpoints = RealtimeEndpointWriteSerializer(many=True)

    class Meta:
        model = RealtimeFeedEntry
        fields = ['protocol', 'license', 'endpoints']

    def validate(self, data):
        protocol = data.get('protocol', '')
        endpoints = data.get('endpoints', [])

        if not endpoints:
            raise serializers.ValidationError(
                {'endpoints': 'At least one endpoint must be provided.'}
            )

        allowed = RealtimeFeedEntry(protocol=protocol).allowed_endpoint_types()
        if not allowed:
            raise serializers.ValidationError(
                {'protocol': f"Unknown protocol '{protocol}'."}
            )

        types_seen = set()
        for ep in endpoints:
            t = ep.get('endpoint_type', '')
            if t not in allowed:
                raise serializers.ValidationError(
                    {'endpoints': f"Endpoint type '{t}' is not valid for protocol '{protocol}'. Allowed: {sorted(allowed)}."}
                )
            if t in types_seen:
                raise serializers.ValidationError(
                    {'endpoints': f"Duplicate endpoint_type '{t}'."}
                )
            types_seen.add(t)
        return data


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
            'id', 'transport_organization', 'data_type', 'feed_kind',
            'name', 'created_at', 'updated_at',
            'current_stage', 'current_stage_label',
            'is_rejected', 'has_rejection_cause',
        ]
        read_only_fields = fields

    def get_has_rejection_cause(self, obj: FeedSubmission) -> bool:
        return bool(obj.rejection_cause)


# ---------------------------------------------------------------------------
# FeedSubmission – detail (owner/admin)
# ---------------------------------------------------------------------------

class FeedSubmissionSerializer(serializers.ModelSerializer):
    static_entries = StaticFeedEntrySerializer(many=True, read_only=True)
    realtime_entry = RealtimeFeedEntrySerializer(read_only=True)
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
            'data_type', 'feed_kind', 'name', 'note',
            'created_at', 'updated_at',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'static_entries', 'realtime_entry',
            'history',
        ]
        read_only_fields = [
            'id', 'feed_kind', 'created_at', 'updated_at',
            'submitted_by',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'history',
        ]


# ---------------------------------------------------------------------------
# FeedSubmission – write (create / update)
# ---------------------------------------------------------------------------

class FeedSubmissionWriteSerializer(serializers.ModelSerializer):
    static_entries = StaticFeedEntrySerializer(required=False, many=True)
    realtime_entry = RealtimeFeedEntryWriteSerializer(required=False, allow_null=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'transport_organization', 'data_type', 'name', 'note',
            'static_entries', 'realtime_entry',
        ]

    def validate(self, data):
        data_type = data.get('data_type') or (self.instance.data_type if self.instance else None)
        is_realtime = data_type in ('gtfs_rt', 'siri', 'gbfs')
        static_entries = data.get('static_entries', None)
        realtime_entry = data.get('realtime_entry', None)

        if self.instance is None:
            has_static = bool(static_entries)
            has_realtime = bool(realtime_entry)
            if is_realtime:
                if not has_realtime:
                    raise serializers.ValidationError(
                        {'realtime_entry': 'Required for GTFS-RT / SIRI / GBFS submissions.'}
                    )
                if has_static:
                    raise serializers.ValidationError(
                        {'static_entries': 'Should not be provided for realtime submissions.'}
                    )
                rt_protocol = realtime_entry.get('protocol', '')
                if rt_protocol != data_type:
                    raise serializers.ValidationError(
                        {'realtime_entry': f"protocol must be '{data_type}', got '{rt_protocol}'."}
                    )
            else:
                if not has_static:
                    raise serializers.ValidationError(
                        {'static_entries': 'At least one static entry is required.'}
                    )
                if has_realtime:
                    raise serializers.ValidationError(
                        {'realtime_entry': 'Should not be provided for static submissions.'}
                    )
            return data

        if static_entries is not None:
            if is_realtime:
                raise serializers.ValidationError(
                    {'static_entries': 'Should not be provided for realtime submissions.'}
                )
            if not static_entries:
                raise serializers.ValidationError(
                    {'static_entries': 'At least one static entry is required.'}
                )
        if realtime_entry is not None:
            if not is_realtime:
                raise serializers.ValidationError(
                    {'realtime_entry': 'Should not be provided for static submissions.'}
                )
            rt_protocol = realtime_entry.get('protocol', '')
            if rt_protocol != data_type:
                raise serializers.ValidationError(
                    {'realtime_entry': f"protocol must be '{data_type}', got '{rt_protocol}'."}
                )
        return data

    @transaction.atomic
    def create(self, validated_data):
        static_entries_data = validated_data.pop('static_entries', None)
        realtime_data = validated_data.pop('realtime_entry', None)

        submission = FeedSubmission(**validated_data)
        submission.save()

        if static_entries_data:
            for static_data in static_entries_data:
                # If cached_file is not set but file is provided -> file upload
                # If cached_file is not set but url is provided -> URL setup (validation will happen after download task)
                # But wait, we want to download immediately if URL is provided and allow validator to run.

                # Check auth_type logic
                if static_data.get('auth_type', 'none') != 'none':
                    static_data['hide_original'] = True

                entry = StaticFeedEntry(submission=submission, **static_data)
                entry.full_clean()
                entry.save()

                # If URL is provided and hide_original is True, we should probably schedule a download task immediately
                # so validation can happen on the downloaded file.
                if entry.url:
                     from data_manager.tasks import fetch_static_entry_task
                     fetch_static_entry_task.delay(entry.id)

        elif realtime_data:
            endpoints_data = realtime_data.pop('endpoints')
            rt_entry = RealtimeFeedEntry(submission=submission, **realtime_data)
            rt_entry.full_clean()
            rt_entry.save()
            for ep_data in endpoints_data:
                if ep_data.get('auth_type', 'none') != 'none':
                    ep_data['hide_original'] = True
                ep = RealtimeEndpoint(entry=rt_entry, **ep_data)
                ep.full_clean()
                ep.save()

        return submission

    @transaction.atomic
    def update(self, instance, validated_data):
        static_entries_data = validated_data.pop('static_entries', None)
        realtime_data = validated_data.pop('realtime_entry', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if static_entries_data is not None:
            instance.static_entries.all().delete()
            for static_data in static_entries_data:
                if static_data.get('auth_type', 'none') != 'none':
                    static_data['hide_original'] = True
                entry = StaticFeedEntry(submission=instance, **static_data)
                entry.full_clean()
                entry.save()
        elif realtime_data:
            endpoints_data = realtime_data.pop('endpoints', [])
            rt_entry, _ = RealtimeFeedEntry.objects.get_or_create(submission=instance)
            for attr, value in realtime_data.items():
                setattr(rt_entry, attr, value)
            rt_entry.full_clean()
            rt_entry.save()
            if endpoints_data:
                rt_entry.endpoints.all().delete()
                for ep_data in endpoints_data:
                    if ep_data.get('auth_type', 'none') != 'none':
                        ep_data['hide_original'] = True
                    ep = RealtimeEndpoint(entry=rt_entry, **ep_data)
                    ep.full_clean()
                    ep.save()

        return instance


# ---------------------------------------------------------------------------
# Admin FeedSubmission serializer – sees everything
# ---------------------------------------------------------------------------

class AdminFeedSubmissionSerializer(FeedSubmissionSerializer):
    """Admin can see submitted_by as username and all stage data."""
    submitted_by_username = serializers.SerializerMethodField()

    class Meta(FeedSubmissionSerializer.Meta):
        fields = FeedSubmissionSerializer.Meta.fields + ['submitted_by_username', 'note']
        read_only_fields = FeedSubmissionSerializer.Meta.read_only_fields + ['submitted_by_username']

    def get_submitted_by_username(self, obj):
        return obj.submitted_by.username if obj.submitted_by else None


# ---------------------------------------------------------------------------
# PUBLIC serializers – for fully approved feeds only
# Strips: auth_value, original hidden URLs, internal stage timestamps,
#         cached_file paths (replaced with a signed download URL).
# ---------------------------------------------------------------------------

class PublishedStaticEntrySerializer(serializers.ModelSerializer):
    """
    For approved static feeds.
    - If hide_original=True → expose cached_file URL via protected download view.
    - If hide_original=False → expose url directly.
    - Never expose auth_value, auth_type, download schedule.
    """
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = StaticFeedEntry
        fields = ['download_url', 'license', 'cached_at']

    def get_download_url(self, obj):
        request = self.context.get('request')
        if obj.hide_original:
            # Serve the server-cached file through our protected view
            if obj.cached_file:
                if request:
                    return request.build_absolute_uri(
                        f'/api/data_manager/feeds/{obj.submission_id}/download/static/{obj.pk}/'
                    )
            return None
        # Not hidden – expose original URL directly
        return obj.url


class PublishedEndpointSerializer(serializers.ModelSerializer):
    """
    For approved realtime endpoints.
    - If hide_original=True → serve via protected proxy URL.
    - If hide_original=False → expose original URL.
    - Never expose auth_value, auth_type.
    """
    feed_url = serializers.SerializerMethodField()

    class Meta:
        model = RealtimeEndpoint
        fields = ['endpoint_type', 'interval', 'feed_url', 'cached_at']

    def get_feed_url(self, obj):
        request = self.context.get('request')
        if obj.hide_original:
            if obj.cached_file:
                if request:
                    return request.build_absolute_uri(
                        f'/api/data_manager/feeds/{obj.entry.submission_id}/download/realtime/{obj.pk}/'
                    )
            return None
        return obj.url


class PublishedRealtimeEntrySerializer(serializers.ModelSerializer):
    endpoints = PublishedEndpointSerializer(many=True, read_only=True)

    class Meta:
        model = RealtimeFeedEntry
        fields = ['protocol', 'license', 'uploaded_at', 'endpoints']


class PublishedFeedSerializer(serializers.ModelSerializer):
    """
    Public-facing serializer for fully approved feeds.
    Only safe fields: org info, data type, dates, download URLs.
    """
    organization_name = serializers.CharField(
        source='transport_organization.transport_organization', read_only=True
    )
    organization_region = serializers.CharField(
        source='transport_organization.region', read_only=True
    )
    static_feeds = PublishedStaticEntrySerializer(source='static_entries', read_only=True, many=True)
    realtime_feed = PublishedRealtimeEntrySerializer(source='realtime_entry', read_only=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'id',
            'organization_name',
            'organization_region',
            'data_type',
            'feed_kind',
            'name',
            'created_at',
            'published_at',
            'static_feeds',
            'realtime_feed',
        ]
        read_only_fields = fields


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
            'feed_kind',
            'name',
            'created_at',
            'static_summary',
            'realtime_endpoint_types',
        ]
        read_only_fields = fields

    def get_static_summary(self, obj):
        entries = getattr(obj, 'static_entries', None)
        if entries is None:
            return {'has_static': False, 'count': 0, 'sources': []}
        entries = entries.all() if hasattr(entries, 'all') else entries
        if not entries:
            return {'has_static': False, 'count': 0, 'sources': []}
        sources = set()
        for entry in entries:
            sources.add('url' if entry.url else 'file')
        return {'has_static': True, 'count': len(entries), 'sources': sorted(sources)}

    def get_realtime_endpoint_types(self, obj):
        entry = getattr(obj, 'realtime_entry', None)
        if not entry:
            return []
        endpoints = getattr(entry, 'endpoints', None)
        if endpoints is None:
            return list(entry.endpoints.values_list('endpoint_type', flat=True))
        return [ep.endpoint_type for ep in endpoints.all()]


# ---------------------------------------------------------------------------
# FeedSubmission – detail (public, no history)
# ---------------------------------------------------------------------------

class FeedDetailSerializer(FeedSubmissionSerializer):
    """Feed detail for public feeds endpoint (no history)."""

    class Meta(FeedSubmissionSerializer.Meta):
        fields = [
            'id', 'transport_organization',
            'submitted_by',
            'data_type', 'feed_kind', 'name', 'note',
            'created_at', 'updated_at',
            'current_stage', 'current_stage_label',
            'is_rejected', 'rejection_cause', 'published_at',
            'static_entries', 'realtime_entry',
        ]
        read_only_fields = FeedSubmissionSerializer.Meta.read_only_fields


# ---------------------------------------------------------------------------
# Organization-level feed serializers
# ---------------------------------------------------------------------------

class OrganizationFeedSubmissionSerializer(serializers.ModelSerializer):
    submitted_by = serializers.SerializerMethodField()
    static_feeds = PublishedStaticEntrySerializer(source='static_entries', many=True, read_only=True)
    realtime_feed = PublishedRealtimeEntrySerializer(source='realtime_entry', read_only=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'id', 'name', 'data_type', 'feed_kind',
            'submitted_by', 'created_at', 'updated_at',
            'static_feeds', 'realtime_feed',
        ]
        read_only_fields = fields

    def get_submitted_by(self, obj):
        return obj.submitted_by.username if obj.submitted_by else None


class OrganizationFeedsSerializer(serializers.ModelSerializer):
    feeds = OrganizationFeedSubmissionSerializer(many=True, read_only=True)

    class Meta:
        model = TransportOrganization
        fields = [
            'id', 'region', 'transport_organization', 'website', 'contact_email', 'phone_number', 'is_public',
            'feeds',
        ]
        read_only_fields = fields


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

    def _feeds(self, obj):
        return getattr(obj, 'feeds', []) or []

    def get_static_types(self, obj):
        return sorted({f.data_type for f in self._feeds(obj) if f.feed_kind == FeedSubmission.FEED_KIND_STATIC})

    def get_dynamic_types(self, obj):
        return sorted({f.data_type for f in self._feeds(obj) if f.feed_kind == FeedSubmission.FEED_KIND_DYNAMIC})


