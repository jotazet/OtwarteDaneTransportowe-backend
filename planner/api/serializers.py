from rest_framework import serializers

from data_manager.models import FeedSubmission, completed_submission_ids
from planner.models import GraphBuild, PlannerFeed


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
            'error_code',
        ]
        read_only_fields = fields


class GraphBuildDetailSerializer(GraphBuildSerializer):
    class Meta(GraphBuildSerializer.Meta):
        fields = [*GraphBuildSerializer.Meta.fields, 'feed_results', 'log_excerpt']
        read_only_fields = fields


class BuildScheduleSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    weekdays = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=7),
        allow_empty=False,
        max_length=7,
    )
    time_utc = serializers.RegexField(r'^([01]\d|2[0-3]):[0-5]\d$')
