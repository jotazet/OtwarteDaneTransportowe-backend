"""Public planner API (/api/planner/v1/). Documented in docs/api/planner.md."""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from planner import planning, services
from planner.access import PlannerPublicView, planner_settings
from planner.api.params import (
    InstantField,
    QueryBoolean,
    agency_filter,
    id_list,
    organization_list,
)
from planner.models import GraphBuild, PlannerFeed

# Routing results are computed on OpenStreetMap data, which must be credited.
OSM_ATTRIBUTION = {
    'source': 'openstreetmap',
    'license': 'ODbL-1.0',
    'url': 'https://www.openstreetmap.org/copyright',
}


class StatusFeedSerializer(serializers.Serializer):
    feed_id = serializers.CharField()
    organization = serializers.DictField()
    license = serializers.CharField(allow_blank=True)
    outcome = serializers.ChoiceField(choices=['fresh', 'stale'])
    version_at = serializers.DateTimeField(allow_null=True)


class StatusSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    reason = serializers.ChoiceField(
        choices=['disabled', 'maintenance', 'no_graph'], allow_null=True
    )
    graph = serializers.DictField(allow_null=True)
    feeds = StatusFeedSerializer(many=True)
    attribution = serializers.ListField(child=serializers.DictField())


class StatusView(PlannerPublicView):
    """Whether the planner answers now, which graph it serves and from which feeds.

    Answers also while the API is switched off or a graph is being built.
    """

    available_when_disabled = True

    @extend_schema(responses=StatusSerializer, summary='Planner availability and data sources')
    def get(self, request):
        active = GraphBuild.objects.filter(is_active=True).select_related('rollback_of').first()
        if not planner_settings().public_api_enabled:
            reason = 'disabled'
        elif services.maintenance_active():
            reason = 'maintenance'
        elif active is None:
            reason = 'no_graph'
        else:
            reason = None
        return Response(
            {
                'available': reason is None,
                'reason': reason,
                'graph': _graph(active),
                'feeds': _feeds(active),
                'attribution': [OSM_ATTRIBUTION],
            }
        )


def _graph(build):
    if build is None:
        return None
    built = build.rollback_of or build
    return {'built_at': built.finished_at, 'otp_version': build.otp_image.rpartition(':')[2]}


def _feeds(build) -> list[dict]:
    if build is None:
        return []
    results = {r['planner_feed']: r for r in build.feed_results if r['outcome'] != 'excluded'}
    feeds = PlannerFeed.objects.filter(id__in=results).select_related(
        'submission__transport_organization', 'submission__static_entry'
    )
    rows = []
    for feed in feeds:
        org = feed.submission.transport_organization
        entry = getattr(feed.submission, 'static_entry', None)
        rows.append(
            {
                'feed_id': feed.feed_id,
                'organization': {
                    'id': org.id,
                    'name': org.transport_organization,
                    'region': org.region,
                },
                'license': (entry.license if entry else '') or '',
                'outcome': results[feed.id]['outcome'],
                'version_at': results[feed.id]['version_at'],
            }
        )
    return sorted(rows, key=lambda row: row['feed_id'])


# ---------------------------------------------------------------------------
# Trip planning
# ---------------------------------------------------------------------------

PLAN_TIMEOUT_SECONDS = 12


class FromFieldMixin:
    """Serves the field declared as `from_` as `from`, a Python keyword."""

    def get_fields(self):
        fields = super().get_fields()
        return {'from' if name == 'from_' else name: field for name, field in fields.items()}


class PlanQuerySerializer(FromFieldMixin, serializers.Serializer):
    from_ = serializers.CharField(
        max_length=128, help_text='`lat,lon` or a stop/station ID, e.g. `krk:2047691`.'
    )
    to = serializers.CharField(max_length=128, help_text='Same as `from`.')
    time = InstantField(required=False, help_text='ISO 8601 with offset; default: now.')
    arrive_by = QueryBoolean(default=False, help_text='`time` is the latest arrival.')
    modes = serializers.ListField(
        child=serializers.ChoiceField(choices=list(planning.TRANSIT_MODES)),
        required=False,
        help_text='Repeat the parameter; default: every transit mode.',
    )
    wheelchair = QueryBoolean(default=False)
    walk_speed = serializers.ChoiceField(choices=list(planning.WALK_SPEEDS), default='normal')
    max_transfers = serializers.IntegerField(min_value=0, max_value=8, required=False)
    min_transfer_minutes = serializers.IntegerField(min_value=0, max_value=30, default=2)
    via = id_list(2)
    agencies = id_list()
    exclude_agencies = id_list()
    organizations = organization_list(help_text='ODT organization IDs: use only their operators.')
    exclude_organizations = organization_list(help_text='ODT organization IDs to leave out.')
    routes = id_list()
    exclude_routes = id_list()
    realtime = QueryBoolean(default=True, help_text='Use realtime delays and cancellations.')
    num_itineraries = serializers.IntegerField(min_value=1, max_value=10, default=5)
    cursor = serializers.CharField(
        max_length=1024, required=False, help_text='`next_cursor` or `previous_cursor` of a result.'
    )
    stops = QueryBoolean(default=False, help_text='List the stops of every transit leg.')
    geometry = QueryBoolean(default=False, help_text="Return every leg's path.")

    def validate_cursor(self, value):
        if not value.startswith((planning.CURSOR_NEXT, planning.CURSOR_PREVIOUS)):
            raise serializers.ValidationError('Unknown cursor.', code='planner.invalid_cursor')
        return value

    def validate(self, attrs):
        for field in ('from', 'to'):
            location = planning.parse_place(attrs[field]).get('coordinate')
            if location:
                valid = -90 <= location['latitude'] <= 90 and -180 <= location['longitude'] <= 180
            else:
                valid = planning.FEED_SCOPED_ID.match(attrs[field]) is not None
            if not valid:
                raise serializers.ValidationError(
                    {field: 'Neither coordinates nor a stop ID.'}, code='planner.invalid_location'
                )
        if attrs['arrive_by'] and not attrs.get('time'):
            raise serializers.ValidationError(
                {'arrive_by': 'Needs time.'}, code='planner.arrive_by_without_time'
            )
        # OTP filters by agency only: an organization stands for all its operators.
        for prefix in ('', 'exclude_'):
            agencies = agency_filter(
                attrs.get(f'{prefix}agencies'),
                attrs.pop(f'{prefix}organizations', None),
                field=f'{prefix}organizations',
            )
            if agencies is not None:
                attrs[f'{prefix}agencies'] = agencies
        return attrs


class PlaceSerializer(serializers.Serializer):
    name = serializers.CharField(allow_null=True)
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    stop_id = serializers.CharField(allow_null=True)
    platform = serializers.CharField(allow_null=True)


class LegTimeSerializer(serializers.Serializer):
    scheduled = serializers.CharField(help_text='UTC instant.')
    estimated = serializers.CharField(allow_null=True, help_text='Realtime estimate, if any.')
    delay_s = serializers.IntegerField(allow_null=True)


class RouteSummarySerializer(serializers.Serializer):
    id = serializers.CharField()
    short_name = serializers.CharField(allow_null=True)
    long_name = serializers.CharField(allow_null=True)
    color = serializers.CharField(allow_null=True)
    text_color = serializers.CharField(allow_null=True)


class AgencySummarySerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()


class AlertSerializer(serializers.Serializer):
    id = serializers.CharField()
    header = serializers.CharField(allow_null=True)
    description = serializers.CharField(allow_null=True)
    url = serializers.CharField(allow_null=True)
    effect = serializers.CharField(allow_null=True)
    severity = serializers.CharField(allow_null=True)


class StopCallSerializer(serializers.Serializer):
    stop_id = serializers.CharField(allow_null=True)
    name = serializers.CharField(allow_null=True)
    lat = serializers.FloatField(allow_null=True)
    lon = serializers.FloatField(allow_null=True)


class LegSerializer(FromFieldMixin, serializers.Serializer):
    mode = serializers.CharField(help_text='walk, bus, tram, rail, subway, …')
    from_ = PlaceSerializer()
    to = PlaceSerializer()
    start = LegTimeSerializer()
    end = LegTimeSerializer()
    duration_s = serializers.IntegerField()
    distance_m = serializers.IntegerField()
    route = RouteSummarySerializer(allow_null=True)
    agency = AgencySummarySerializer(allow_null=True)
    trip_id = serializers.CharField(allow_null=True)
    service_date = serializers.DateField(allow_null=True)
    headsign = serializers.CharField(allow_null=True)
    realtime = serializers.BooleanField()
    geometry = serializers.CharField(allow_null=True, help_text='Encoded polyline (precision 5).')
    alerts = AlertSerializer(many=True)
    stops = StopCallSerializer(many=True, allow_null=True)


class ItinerarySerializer(serializers.Serializer):
    start = serializers.CharField()
    end = serializers.CharField()
    duration_s = serializers.IntegerField()
    walk_distance_m = serializers.IntegerField()
    transfers = serializers.IntegerField()
    legs = LegSerializer(many=True)


class RoutingErrorSerializer(serializers.Serializer):
    code = serializers.CharField()


class PlanResponseSerializer(serializers.Serializer):
    itineraries = ItinerarySerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True)
    previous_cursor = serializers.CharField(allow_null=True)
    routing_errors = RoutingErrorSerializer(many=True)


class PlanView(PlannerPublicView):
    """Plan a trip by public transport, with realtime delays where the operators provide them."""

    limit_group = 'plan'
    uses_otp = True

    def otp_input_errors(self, messages):
        return planning.input_errors(messages)

    @extend_schema(
        parameters=[PlanQuerySerializer],
        responses=PlanResponseSerializer,
        summary='Plan a trip',
    )
    def get(self, request):
        data = self.otp_query(
            planning.PLAN_DOCUMENT,
            planning.plan_variables(self.validated(PlanQuerySerializer)),
            timeout=PLAN_TIMEOUT_SECONDS,
        )
        return Response(PlanResponseSerializer(planning.plan_response(data)).data)
