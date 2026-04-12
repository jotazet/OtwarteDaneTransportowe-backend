from django.db import transaction
from django.db.models import OuterRef, Subquery
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
# FeedFetchError
# ---------------------------------------------------------------------------

class FeedFetchErrorSerializer(serializers.ModelSerializer):
    source = serializers.SerializerMethodField()
    endpoint_type = serializers.SerializerMethodField()

    class Meta:
        model = FeedFetchError
        fields = [
            'id', 'source', 'static_entry', 'endpoint_rt', 'endpoint_type',
            'error_type', 'http_status_code', 'message', 'url_attempted', 'occurred_at',
        ]
        read_only_fields = fields

    def get_source(self, obj):
        return 'static' if obj.static_entry_id else 'realtime'

    def get_endpoint_type(self, obj):
        return obj.endpoint_rt.endpoint_type if obj.endpoint_rt else None


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
            'auth_type': {'required': False, 'allow_null': True},
        }


# ---------------------------------------------------------------------------
# RealtimeSubmission – realtime flow
# ---------------------------------------------------------------------------

class RealtimeEndpointRTSerializer(serializers.ModelSerializer):
    class Meta:
        model = RealtimeEndpointRT
        fields = [
            'id', 'endpoint_type', 'url', 'is_original',
            'hide_original', 'auth_type', 'auth_value',
            'interval', 'cached_at',
        ]
        read_only_fields = ['id', 'cached_at']
        extra_kwargs = {'auth_value': {'write_only': True}}


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
            2: 'Step 2: Validated',
            3: 'Step 3: Admin confirmation',
            4: 'Step 4: Published',
        }
        return labels.get(obj.current_stage, 'Unknown')


class RealtimeSubmissionWriteSerializer(serializers.ModelSerializer):
    endpoints = RealtimeEndpointRTWriteSerializer(many=True)
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

    def validate(self, data):
        from data_manager.models import completed_submission_ids

        inst = getattr(self, 'instance', None)
        protocol = data.get('protocol', inst.protocol if inst else '')
        org = data.get('transport_organization', inst.transport_organization if inst else None)
        ss = data.get('static_submission', inst.static_submission if inst else None)
        endpoints = data.get('endpoints')
        if endpoints is not None and not endpoints:
            raise serializers.ValidationError({'endpoints': 'At least one endpoint is required.'})
        if endpoints is None and inst is None:
            raise serializers.ValidationError({'endpoints': 'At least one endpoint is required.'})

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

class FeedSubmissionWriteSerializer(serializers.ModelSerializer):
    static_entry = StaticFeedEntrySerializer(required=False, allow_null=True)

    class Meta:
        model = FeedSubmission
        fields = [
            'transport_organization', 'data_type', 'name', 'note',
            'static_entry',
        ]

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
                from data_manager.tasks import fetch_static_entry_task
                fetch_static_entry_task.delay(entry.id)

        return submission

    @transaction.atomic
    def update(self, instance, validated_data):
        static_entry_data = validated_data.pop('static_entry', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if static_entry_data:
            if hasattr(instance, 'static_entry') and instance.static_entry:
                entry = instance.static_entry
                for attr, value in static_entry_data.items():
                    setattr(entry, attr, value)
                if static_entry_data.get('auth_type') is not None:
                    entry.hide_original = True
                entry.full_clean()
                entry.save()
            else:
                if static_entry_data.get('auth_type') is not None:
                    static_entry_data['hide_original'] = True
                entry = StaticFeedEntry(submission=instance, **static_entry_data)
                entry.full_clean()
                entry.save()

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
        fields = ['download_url', 'license', 'cached_at']

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
        fields = ['endpoint_type', 'interval', 'feed_url', 'cached_at']

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


class OrganizationFeedsSerializer(serializers.ModelSerializer):
    feeds = OrganizationFeedSubmissionSerializer(many=True, read_only=True)
    gbfs_feeds = serializers.SerializerMethodField()

    class Meta:
        model = TransportOrganization
        fields = [
            'id', 'region', 'transport_organization', 'website', 'contact_email', 'phone_number', 'is_public',
            'feeds', 'gbfs_feeds',
        ]
        read_only_fields = fields

    def get_gbfs_feeds(self, obj):
        feeds = getattr(obj, 'published_gbfs_feeds', None) or []
        return PublishedGbfsFeedSerializer(
            feeds, many=True, context=self.context
        ).data


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
        gbfs = getattr(obj, 'published_gbfs_feeds', None) or []
        return ['gbfs'] if gbfs else []


class AdminRealtimeSubmissionSerializer(RealtimeSubmissionSerializer):
    submitted_by_username = serializers.SerializerMethodField()

    class Meta(RealtimeSubmissionSerializer.Meta):
        fields = RealtimeSubmissionSerializer.Meta.fields + ['submitted_by_username']
        read_only_fields = RealtimeSubmissionSerializer.Meta.read_only_fields + ['submitted_by_username']

    def get_submitted_by_username(self, obj):
        return obj.submitted_by.username if obj.submitted_by else None
