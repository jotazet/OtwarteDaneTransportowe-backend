from django.conf import settings
from rest_framework import serializers

from data_manager.models import FeedSubmission, RealtimeEndpoint, RealtimeFeedEntry, StaticFeedEntry


# ---------------------------------------------------------------------------
# StaticFeedEntry – private (owner sees everything except auth_value)
# ---------------------------------------------------------------------------

class StaticFeedEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StaticFeedEntry
        fields = [
            'id', 'url', 'file', 'hide_original',
            'auth_type', 'auth_value',
            'download_time_1', 'download_time_2',
            'cached_at', 'uploaded_at',
        ]
        read_only_fields = ['id', 'cached_at', 'uploaded_at']
        extra_kwargs = {'auth_value': {'write_only': True}}


# ---------------------------------------------------------------------------
# RealtimeEndpoint / RealtimeFeedEntry – private
# ---------------------------------------------------------------------------

class RealtimeEndpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = RealtimeEndpoint
        fields = [
            'id', 'endpoint_type', 'url',
            'hide_original', 'auth_type', 'auth_value',
            'cached_at',
        ]
        read_only_fields = ['id', 'cached_at']
        extra_kwargs = {'auth_value': {'write_only': True}}


class RealtimeFeedEntrySerializer(serializers.ModelSerializer):
    endpoints = RealtimeEndpointSerializer(many=True, read_only=True)

    class Meta:
        model = RealtimeFeedEntry
        fields = ['id', 'protocol', 'uploaded_at', 'endpoints']
        read_only_fields = ['id', 'uploaded_at']


class RealtimeFeedEntryWriteSerializer(serializers.ModelSerializer):
    endpoints = RealtimeEndpointSerializer(many=True)

    class Meta:
        model = RealtimeFeedEntry
        fields = ['protocol', 'endpoints']

    def validate(self, data):
        protocol = data.get('protocol', '')
        endpoints = data.get('endpoints', [])

        if not endpoints:
            raise serializers.ValidationError(
                {'endpoints': 'At least one endpoint must be provided.'}
            )

        allowed = (
            RealtimeFeedEntry.GTFS_RT_ENDPOINT_TYPES
            if protocol == RealtimeFeedEntry.PROTOCOL_GTFS_RT
            else RealtimeFeedEntry.SIRI_ENDPOINT_TYPES
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
# FeedSubmission – private detail (owner)
# ---------------------------------------------------------------------------

class FeedSubmissionSerializer(serializers.ModelSerializer):
    static_entry = StaticFeedEntrySerializer(read_only=True)
    realtime_entry = RealtimeFeedEntrySerializer(read_only=True)
    current_stage = serializers.IntegerField(read_only=True)
    current_stage_label = serializers.CharField(read_only=True)
    submitted_by_username = serializers.SerializerMethodField()

    class Meta:
        model = FeedSubmission
        fields = [
            'id', 'transport_organization',
            'submitted_by', 'submitted_by_username',
            'data_type', 'feed_kind', 'name', 'note',
            'created_at', 'updated_at',
            'stage_upload_at', 'stage_verification_at',
            'stage_confirmation_at', 'stage_complete_at',
            'current_stage', 'current_stage_label',
            'static_entry', 'realtime_entry',
        ]
        read_only_fields = [
            'id', 'feed_kind', 'created_at', 'updated_at',
            'submitted_by', 'submitted_by_username',
            'stage_upload_at', 'stage_verification_at',
            'stage_confirmation_at', 'stage_complete_at',
            'current_stage', 'current_stage_label',
        ]

    def get_submitted_by_username(self, obj):
        return obj.submitted_by.username if obj.submitted_by else None


class FeedSubmissionListSerializer(serializers.ModelSerializer):
    current_stage = serializers.IntegerField(read_only=True)
    current_stage_label = serializers.CharField(read_only=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'id', 'transport_organization', 'data_type', 'feed_kind',
            'name', 'created_at', 'updated_at',
            'current_stage', 'current_stage_label',
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# FeedSubmission – write (create)
# ---------------------------------------------------------------------------

class FeedSubmissionWriteSerializer(serializers.ModelSerializer):
    static_entry = StaticFeedEntrySerializer(required=False)
    realtime_entry = RealtimeFeedEntryWriteSerializer(required=False)

    class Meta:
        model = FeedSubmission
        fields = [
            'transport_organization', 'data_type', 'name', 'note',
            'static_entry', 'realtime_entry',
        ]

    def validate(self, data):
        data_type = data.get('data_type', '')
        is_realtime = data_type in ('gtfs_rt', 'siri')
        has_static = bool(data.get('static_entry'))
        has_realtime = bool(data.get('realtime_entry'))

        if is_realtime:
            if not has_realtime:
                raise serializers.ValidationError(
                    {'realtime_entry': 'Required for GTFS-RT / SIRI submissions.'}
                )
            if has_static:
                raise serializers.ValidationError(
                    {'static_entry': 'Should not be provided for realtime submissions.'}
                )
            rt_protocol = data['realtime_entry'].get('protocol', '')
            if rt_protocol != data_type:
                raise serializers.ValidationError(
                    {'realtime_entry': f"protocol must be '{data_type}', got '{rt_protocol}'."}
                )
        else:
            if not has_static:
                raise serializers.ValidationError(
                    {'static_entry': 'Required for static feed submissions.'}
                )
            if has_realtime:
                raise serializers.ValidationError(
                    {'realtime_entry': 'Should not be provided for static submissions.'}
                )
        return data

    def create(self, validated_data):
        static_data = validated_data.pop('static_entry', None)
        realtime_data = validated_data.pop('realtime_entry', None)

        request = self.context.get('request')
        if request and request.user and request.user.is_authenticated:
            validated_data['submitted_by'] = request.user

        submission = FeedSubmission(**validated_data)
        submission.save()

        if static_data:
            entry = StaticFeedEntry(submission=submission, **static_data)
            entry.full_clean()
            entry.save()
        elif realtime_data:
            endpoints_data = realtime_data.pop('endpoints')
            rt_entry = RealtimeFeedEntry(submission=submission, **realtime_data)
            rt_entry.full_clean()
            rt_entry.save()
            for ep_data in endpoints_data:
                ep = RealtimeEndpoint(entry=rt_entry, **ep_data)
                ep.full_clean()
                ep.save()

        from django.utils import timezone
        FeedSubmission.objects.filter(pk=submission.pk).update(stage_upload_at=timezone.now())

        return submission


# ---------------------------------------------------------------------------
# PUBLIC serializers – for fully approved feeds only
# Strips: auth_value, original hidden URLs, internal stage timestamps,
#         cached_file paths (replaced with a signed download URL).
# ---------------------------------------------------------------------------

class PublishedStaticEntrySerializer(serializers.ModelSerializer):
    """
    For approved static feeds.
    - If hide_original=True → expose cached_file URL, never the original url.
    - If hide_original=False → expose url directly.
    - Never expose auth_value, auth_type, download schedule.
    """
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = StaticFeedEntry
        fields = ['download_url', 'uploaded_at', 'cached_at']

    def get_download_url(self, obj):
        request = self.context.get('request')
        if obj.hide_original:
            # Serve the server-cached file through our protected view
            if obj.cached_file:
                return request.build_absolute_uri(
                    f'/api/data_manager/feeds/download/static/{obj.pk}/'
                ) if request else None
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
        fields = ['endpoint_type', 'feed_url', 'cached_at']

    def get_feed_url(self, obj):
        request = self.context.get('request')
        if obj.hide_original:
            if obj.cached_file:
                return request.build_absolute_uri(
                    f'/api/data_manager/feeds/download/realtime/{obj.pk}/'
                ) if request else None
            return None
        return obj.url


class PublishedRealtimeEntrySerializer(serializers.ModelSerializer):
    endpoints = PublishedEndpointSerializer(many=True, read_only=True)

    class Meta:
        model = RealtimeFeedEntry
        fields = ['protocol', 'uploaded_at', 'endpoints']


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
    static_feed = PublishedStaticEntrySerializer(source='static_entry', read_only=True)
    realtime_feed = PublishedRealtimeEntrySerializer(source='realtime_entry', read_only=True)
    published_at = serializers.DateTimeField(source='stage_complete_at', read_only=True)

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
            'updated_at',
            'published_at',
            'static_feed',
            'realtime_feed',
        ]
        read_only_fields = fields

