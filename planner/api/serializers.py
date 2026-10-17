import ipaddress
from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from data_manager.models import (
    FeedSubmission,
    RealtimeSubmission,
    completed_realtime_submission_ids,
    completed_submission_ids,
)
from data_manager.net_security import OutboundURLBlocked, assert_safe_outbound_url
from planner import otp_config, request_logs
from planner.models import (
    ApiKey,
    GraphBuild,
    IpBan,
    OsmDownload,
    PlannerFeed,
    PlannerRentalSource,
    PlannerRequestLog,
    PlannerSettings,
)


def _organization(submission: FeedSubmission) -> dict:
    org = submission.transport_organization
    return {'id': org.id, 'name': org.transport_organization, 'region': org.region}


class PlannerFeedSerializer(serializers.ModelSerializer):
    submission_name = serializers.CharField(source='submission.name', read_only=True)
    organization = serializers.SerializerMethodField()
    active_result = serializers.SerializerMethodField()

    class Meta:
        model = PlannerFeed
        fields = [
            'id',
            'submission',
            'submission_name',
            'organization',
            'feed_id',
            'is_enabled',
            'include_realtime',
            'realtime_from',
            'failure_policy',
            'last_good_at',
            'active_result',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['last_good_at', 'created_at', 'updated_at']

    def get_organization(self, obj):
        return _organization(obj.submission)

    def get_active_result(self, obj):
        """This feed's outcome in the build being served (fresh, stale or excluded)."""
        return self.context.get('active_results', {}).get(obj.id)

    def validate_feed_id(self, value):
        if self.instance is not None and value != self.instance.feed_id:
            # Every stop, route and trip ID served by the planner starts with it.
            raise serializers.ValidationError(
                'feed_id cannot change.', code='planner.feed_id_immutable'
            )
        return value

    def validate_submission(self, submission):
        if submission.data_type != 'gtfs':
            raise serializers.ValidationError('Only GTFS feeds.', code='planner.feed_not_gtfs')
        if submission.id not in completed_submission_ids():
            raise serializers.ValidationError(
                'The feed is not published.', code='planner.feed_not_published'
            )
        if (
            self.instance is not None
            and submission.transport_organization_id
            != self.instance.submission.transport_organization_id
        ):
            raise serializers.ValidationError(
                'A planner feed can only move to a feed of the same organization.',
                code='planner.feed_other_organization',
            )
        return submission

    def validate(self, attrs):
        # An unchanged realtime_from stays valid: the submission only moves within its organization.
        realtime_from = attrs.get('realtime_from')
        submission = attrs.get('submission') or self.instance.submission
        if (
            realtime_from is not None
            and realtime_from.transport_organization_id != submission.transport_organization_id
        ):
            raise serializers.ValidationError(
                {'realtime_from': 'Realtime only from a feed of the same organization.'},
                code='planner.feed_other_organization',
            )
        return attrs


class PlannerRentalSourceSerializer(serializers.ModelSerializer):
    submission_name = serializers.CharField(source='submission.name', read_only=True)
    organization = serializers.SerializerMethodField()
    active_result = serializers.SerializerMethodField()

    class Meta:
        model = PlannerRentalSource
        fields = [
            'id',
            'submission',
            'submission_name',
            'organization',
            'network',
            'is_enabled',
            'active_result',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_organization(self, obj):
        return _organization(obj.submission)

    def get_active_result(self, obj):
        """This source's outcome in the build being served (included or skipped)."""
        return self.context.get('active_results', {}).get(obj.id)

    def validate_network(self, value):
        if self.instance is not None and value != self.instance.network:
            # Every station and vehicle ID served by the planner starts with it.
            raise serializers.ValidationError(
                'network cannot change.', code='planner.network_immutable'
            )
        return value

    def validate_submission(self, submission):
        if submission.protocol != RealtimeSubmission.PROTOCOL_GBFS:
            raise serializers.ValidationError('Only GBFS feeds.', code='planner.feed_not_gbfs')
        if submission.id not in completed_realtime_submission_ids():
            raise serializers.ValidationError(
                'The feed is not published.', code='planner.feed_not_published'
            )
        endpoint = submission.endpoints.filter(endpoint_type='gbfs').first()
        if endpoint is None:
            raise serializers.ValidationError(
                'No gbfs.json endpoint.', code='planner.rental_source_without_gbfs'
            )
        if otp_config.gbfs_needs_credentials(endpoint):
            raise serializers.ValidationError(
                'Needs credentials.', code='planner.rental_source_requires_auth'
            )
        return submission


class RentalCandidateSerializer(serializers.ModelSerializer):
    """A published GBFS submission, with its rental source if it is already included."""

    organization = serializers.SerializerMethodField()
    rental_source = serializers.SerializerMethodField()

    class Meta:
        model = RealtimeSubmission
        fields = ['id', 'name', 'organization', 'rental_source']

    def get_organization(self, obj):
        return _organization(obj)

    def get_rental_source(self, obj):
        source = getattr(obj, 'planner_rental_source', None)
        return source.id if source else None


class FeedCandidateSerializer(serializers.ModelSerializer):
    """A published GTFS submission, with its planner feed if it is already included."""

    organization = serializers.SerializerMethodField()
    planner_feed = serializers.SerializerMethodField()

    class Meta:
        model = FeedSubmission
        fields = ['id', 'name', 'organization', 'planner_feed']

    def get_organization(self, obj):
        return _organization(obj)

    def get_planner_feed(self, obj):
        feed = getattr(obj, 'planner_feed', None)
        return feed.id if feed else None


class GraphBuildSerializer(serializers.ModelSerializer):
    requested_by = serializers.CharField(source='requested_by.username', default=None)

    class Meta:
        model = GraphBuild
        fields = [
            'id',
            'status',
            'trigger',
            'requested_by',
            'rollback_of',
            'otp_image',
            'created_at',
            'started_at',
            'otp_stopped_at',
            'finished_at',
            'is_active',
            'indexed_at',
            'error_code',
        ]
        read_only_fields = fields


class GraphBuildDetailSerializer(GraphBuildSerializer):
    class Meta(GraphBuildSerializer.Meta):
        fields = [
            *GraphBuildSerializer.Meta.fields,
            'feed_results',
            'rental_results',
            'log_excerpt',
        ]
        read_only_fields = fields


class OsmDownloadSerializer(serializers.ModelSerializer):
    requested_by = serializers.CharField(source='requested_by.username', default=None)

    class Meta:
        model = OsmDownload
        fields = [
            'id',
            'url',
            'status',
            'error_code',
            'size_bytes',
            'data_timestamp',
            'requested_by',
            'created_at',
            'started_at',
            'finished_at',
        ]
        read_only_fields = fields


class OsmDownloadRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = OsmDownload
        fields = ['url']

    def validate_url(self, value):
        # Checked again (and pinned) when the worker downloads it.
        try:
            assert_safe_outbound_url(value)
        except OutboundURLBlocked:
            raise serializers.ValidationError(
                'Not a public address.', code='planner.osm_url_blocked'
            ) from None
        return value


class BuildScheduleSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    weekdays = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=7),
        allow_empty=False,
        max_length=7,
    )
    time_utc = serializers.RegexField(r'^([01]\d|2[0-3]):[0-5]\d$')


class PlannerSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlannerSettings
        exclude = ['id', 'updated_by']
        read_only_fields = ['updated_at']


class ApiKeySerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source='created_by.username', default=None, read_only=True)

    class Meta:
        model = ApiKey
        fields = [
            'id',
            'name',
            'contact_email',
            'note',
            'prefix',
            'is_active',
            'plan_per_minute',
            'realtime_per_minute',
            'lookup_per_minute',
            'created_by',
            'created_at',
            'last_used_at',
        ]
        read_only_fields = ['prefix', 'created_at', 'last_used_at']


class IpBanSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source='created_by.username', default=None, read_only=True)

    class Meta:
        model = IpBan
        fields = ['id', 'network', 'reason', 'expires_at', 'created_by', 'created_at']
        read_only_fields = ['created_at']

    def validate_network(self, value):
        try:
            network = str(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            raise serializers.ValidationError(
                'Not an IP address or network.', code='planner.invalid_network'
            ) from None
        # The model's unique check saw the input before normalisation.
        duplicates = IpBan.objects.filter(network=network)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError('Already banned.', code='unique')
        return network


class PlannerRequestLogSerializer(serializers.ModelSerializer):
    api_key_name = serializers.CharField(source='api_key.name', default=None)

    class Meta:
        model = PlannerRequestLog
        fields = [
            'id',
            'created_at',
            'endpoint',
            'status_code',
            'latency_ms',
            'client_ip',
            'api_key',
            'api_key_name',
            'user_agent',
        ]
        read_only_fields = fields


class DailyStatsFilterSerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    endpoint = serializers.CharField(required=False)
    api_key = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if (
            attrs.get('date_from')
            and attrs.get('date_to')
            and attrs['date_from'] > attrs['date_to']
        ):
            raise serializers.ValidationError(
                'date_from is after date_to.', code='planner.invalid_range'
            )
        return attrs


class StatsFilterSerializer(serializers.Serializer):
    """Filters of the raw-log statistics; the default range is the last 24 hours."""

    since = serializers.DateTimeField(required=False)
    until = serializers.DateTimeField(required=False)
    endpoint = serializers.CharField(required=False, help_text='URL name, e.g. planner-v1-plan.')
    api_key = serializers.IntegerField(required=False)
    ip = serializers.IPAddressField(required=False)

    def validate(self, attrs):
        attrs['until'] = attrs.get('until') or timezone.now()
        attrs['since'] = attrs.get('since') or attrs['until'] - timedelta(days=1)
        if attrs['since'] >= attrs['until']:
            raise serializers.ValidationError(
                {'since': 'since is not before until.'}, code='planner.invalid_range'
            )
        return attrs


class RequestLogFilterSerializer(StatsFilterSerializer):
    status = serializers.IntegerField(required=False, min_value=100, max_value=599)


class TimeseriesFilterSerializer(StatsFilterSerializer):
    interval = serializers.ChoiceField(choices=list(request_logs.INTERVALS), default='hour')
    split = serializers.ChoiceField(choices=['status', 'endpoint', 'none'], default='status')

    def validate(self, attrs):
        attrs = super().validate(attrs)
        buckets = request_logs.bucket_count(attrs['since'], attrs['until'], attrs['interval'])
        if buckets > request_logs.MAX_BUCKETS:
            raise serializers.ValidationError(
                {'interval': 'Too many points for this range.'}, code='planner.too_many_buckets'
            )
        return attrs


class TopClientsFilterSerializer(StatsFilterSerializer):
    by = serializers.ChoiceField(choices=['ip', 'api_key'], default='ip')
