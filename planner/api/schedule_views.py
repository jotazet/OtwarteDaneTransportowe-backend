"""Departures, stop timetables, lines and trips of the public planner API (docs/api/planner.md).

Answered by OTP, so with realtime data where operators publish it, and
unavailable while OTP is stopped for a graph build.
"""

from datetime import timedelta
from functools import partial

from django.core.validators import RegexValidator
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from planner import alerts, otp_runtime, rentals, schedules, vehicles
from planner.access import OTP_BUSY_RETRY_AFTER_SECONDS, PlannerError, PlannerPublicView
from planner.api.lookup_views import LookupListView, MapPagination
from planner.api.params import (
    MAX_IDS,
    BboxField,
    FeedScopedIdField,
    InstantField,
    OperatorFilter,
    QueryBoolean,
    agency_filter,
    id_list,
    organization_list,
)
from planner.api.public_views import AgencySummarySerializer
from planner.models import SCOPE_ID_PATTERN, GraphBuild, IndexedBlockTrip, IndexedRoute

SCHEDULE_TIMEOUT_SECONDS = 10
STATUSES = ['scheduled', 'updated', 'canceled', 'added', 'modified']
# Vehicles are filtered in memory, so a larger area than for stops costs nothing.
VEHICLE_MAX_BBOX_DEGREES = 1.0
# OTP's FormFactor values (GBFS form_factor), lowercase.
FORM_FACTORS = [
    'bicycle',
    'cargo_bicycle',
    'car',
    'moped',
    'scooter',
    'scooter_standing',
    'scooter_seated',
    'other',
]


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class RouteRefSerializer(serializers.Serializer):
    id = serializers.CharField()
    short_name = serializers.CharField(allow_null=True)
    long_name = serializers.CharField(allow_null=True)
    mode = serializers.CharField()
    color = serializers.CharField(allow_null=True)
    text_color = serializers.CharField(allow_null=True)
    agency = AgencySummarySerializer(allow_null=True)


class StopRefSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    platform = serializers.CharField(allow_null=True)


class DepartureSerializer(serializers.Serializer):
    stop_id = serializers.CharField()
    platform = serializers.CharField(allow_null=True)
    route = RouteRefSerializer()
    trip_id = serializers.CharField()
    service_date = serializers.DateField()
    headsign = serializers.CharField(allow_null=True)
    scheduled = serializers.CharField(help_text='UTC instant.')
    estimated = serializers.CharField(allow_null=True, help_text='Realtime prediction, if any.')
    delay_s = serializers.IntegerField(allow_null=True)
    realtime = serializers.BooleanField()
    status = serializers.ChoiceField(choices=STATUSES)


class DeparturesSerializer(serializers.Serializer):
    departures = DepartureSerializer(many=True)


class TimetableDepartureSerializer(serializers.Serializer):
    time = serializers.CharField(help_text='Scheduled departure, UTC instant.')
    trip_id = serializers.CharField()
    canceled = serializers.BooleanField()


class TimetableLineSerializer(serializers.Serializer):
    route = RouteRefSerializer()
    headsign = serializers.CharField(allow_null=True)
    departures = TimetableDepartureSerializer(many=True)


class TimetableStopSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    platform = serializers.CharField(allow_null=True)


class StopTimetableSerializer(serializers.Serializer):
    stop = TimetableStopSerializer()
    date = serializers.DateField()
    lines = TimetableLineSerializer(many=True)


class RouteVariantSerializer(serializers.Serializer):
    headsign = serializers.CharField(allow_null=True)
    direction_id = serializers.IntegerField(allow_null=True)
    stops = StopRefSerializer(many=True)
    geometry = serializers.CharField(allow_null=True, help_text='With `geometry=true`.')


class RouteDetailSerializer(RouteRefSerializer):
    variants = RouteVariantSerializer(many=True)


class CallTimeSerializer(serializers.Serializer):
    scheduled = serializers.CharField(allow_null=True)
    estimated = serializers.CharField(allow_null=True)
    delay_s = serializers.IntegerField(allow_null=True)


class CallSerializer(serializers.Serializer):
    stop = StopRefSerializer()
    arrival = CallTimeSerializer()
    departure = CallTimeSerializer()


class TripDetailSerializer(serializers.Serializer):
    id = serializers.CharField()
    headsign = serializers.CharField(allow_null=True)
    direction_id = serializers.IntegerField(allow_null=True)
    block_id = serializers.CharField(allow_null=True, help_text="The vehicle's block (brigade).")
    route = RouteRefSerializer()
    service_date = serializers.DateField()
    realtime = serializers.BooleanField()
    calls = CallSerializer(many=True)
    geometry = serializers.CharField(allow_null=True, help_text='With `geometry=true`.')


class VehicleStopSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()


class VehicleSerializer(serializers.Serializer):
    id = serializers.CharField(help_text='Feed-scoped vehicle ID.')
    label = serializers.CharField(allow_null=True, help_text='Number shown on the vehicle.')
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    heading = serializers.FloatField(allow_null=True, help_text='Degrees clockwise from north.')
    speed_m_s = serializers.FloatField(allow_null=True)
    updated_at = serializers.CharField(allow_null=True, help_text='UTC instant of the position.')
    route = RouteRefSerializer()
    headsign = serializers.CharField(allow_null=True)
    trip_id = serializers.CharField(allow_null=True)
    block_id = serializers.CharField(allow_null=True)
    stop_status = serializers.ChoiceField(
        choices=['incoming_at', 'stopped_at', 'in_transit_to'], allow_null=True
    )
    stop = VehicleStopSerializer(allow_null=True, help_text='The stop `stop_status` refers to.')


class BlockCallSerializer(serializers.Serializer):
    stop = StopRefSerializer()
    scheduled = serializers.CharField(allow_null=True)
    estimated = serializers.CharField(allow_null=True)
    delay_s = serializers.IntegerField(allow_null=True)


class BlockTripSerializer(serializers.Serializer):
    id = serializers.CharField()
    route = RouteRefSerializer()
    headsign = serializers.CharField(allow_null=True)
    start = BlockCallSerializer(help_text='First stop, departure.')
    end = BlockCallSerializer(help_text='Last stop, arrival.')
    realtime = serializers.BooleanField()
    current = serializers.BooleanField(help_text='Under way now.')


class BlockSerializer(serializers.Serializer):
    feed_id = serializers.CharField()
    block_id = serializers.CharField()
    service_date = serializers.DateField()
    trips = BlockTripSerializer(many=True)


class ActivePeriodSerializer(serializers.Serializer):
    start = serializers.CharField(allow_null=True)
    end = serializers.CharField(allow_null=True)


class ServiceAlertSerializer(serializers.Serializer):
    id = serializers.CharField()
    feed_id = serializers.CharField(allow_null=True)
    header = serializers.CharField(allow_null=True, help_text="The operator's text.")
    description = serializers.CharField(allow_null=True, help_text="The operator's text.")
    url = serializers.CharField(allow_null=True)
    effect = serializers.CharField(allow_null=True, help_text='GTFS-RT effect, lowercase.')
    cause = serializers.CharField(allow_null=True, help_text='GTFS-RT cause, lowercase.')
    severity = serializers.CharField(allow_null=True, help_text='info, warning, severe, …')
    active = serializers.BooleanField(help_text='In force now.')
    active_periods = ActivePeriodSerializer(many=True)
    agencies = serializers.ListField(child=serializers.CharField())
    routes = serializers.ListField(child=serializers.CharField())
    stops = serializers.ListField(child=serializers.CharField())
    trips = serializers.ListField(child=serializers.CharField())


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class DeparturesParams(serializers.Serializer):
    id = serializers.ListField(
        child=FeedScopedIdField(),
        min_length=1,
        max_length=MAX_IDS,
        help_text='Stop ID; repeat for every pole of a place (from stop search).',
    )
    time = InstantField(required=False, help_text='From when, with UTC offset; default now.')
    window = serializers.IntegerField(
        min_value=1, max_value=720, default=60, help_text='Minutes after `time`.'
    )
    limit = serializers.IntegerField(min_value=1, max_value=50, default=10)
    routes = id_list(help_text='Only these lines.')


class StopTimetableParams(serializers.Serializer):
    id = FeedScopedIdField(help_text='Stop ID.')
    date = serializers.DateField(required=False, help_text='Service date; default today in Poland.')
    routes = id_list(help_text='Only these lines.')


class RouteParams(serializers.Serializer):
    id = FeedScopedIdField(help_text='Line ID, e.g. `krk:352529`.')
    geometry = QueryBoolean(default=False, help_text="Return every variant's path.")


class TripParams(serializers.Serializer):
    id = FeedScopedIdField(help_text='Trip ID, e.g. from a departure or a plan leg.')
    date = serializers.DateField(
        required=False,
        help_text='Service date (`service_date`); default: the day of the run under way or next.',
    )
    geometry = QueryBoolean(default=False, help_text="Return the trip's path.")


class VehicleParams(OperatorFilter):
    bbox = BboxField(
        VEHICLE_MAX_BBOX_DEGREES,
        required=False,
        help_text='`min_lon,min_lat,max_lon,max_lat`, at most 1° a side.',
    )
    routes = id_list(help_text='Only these lines.')


class RentalVehicleTypeSerializer(serializers.Serializer):
    form_factor = serializers.CharField(allow_null=True)
    propulsion = serializers.CharField(allow_null=True)
    count = serializers.IntegerField(allow_null=True)


class RentalPlaceSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=['station', 'vehicle'])
    id = serializers.CharField(help_text='`<network>:<id>`.')
    network = serializers.CharField(allow_null=True)
    name = serializers.CharField(allow_null=True)
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    vehicles_available = serializers.IntegerField(allow_null=True, help_text='Stations only.')
    spaces_available = serializers.IntegerField(allow_null=True, help_text='Stations only.')
    vehicle_types = RentalVehicleTypeSerializer(many=True)
    pickup_now = serializers.BooleanField(allow_null=True)
    dropoff_now = serializers.BooleanField(allow_null=True, help_text='Stations only.')
    battery_percent = serializers.IntegerField(allow_null=True, help_text='Vehicles only.')
    range_m = serializers.IntegerField(allow_null=True, help_text='Vehicles only.')


class RentalParams(serializers.Serializer):
    bbox = BboxField(
        VEHICLE_MAX_BBOX_DEGREES, help_text='`min_lon,min_lat,max_lon,max_lat`, at most 1° a side.'
    )
    networks = serializers.ListField(
        child=serializers.CharField(
            validators=[RegexValidator(SCOPE_ID_PATTERN, code='planner.invalid_network')]
        ),
        max_length=MAX_IDS,
        required=False,
        help_text='Only these sharing systems.',
    )
    form_factors = serializers.ListField(
        child=serializers.ChoiceField(choices=FORM_FACTORS),
        required=False,
        help_text='Only these vehicle kinds; repeat.',
    )


class CoverageSerializer(serializers.Serializer):
    agency = AgencySummarySerializer()
    vehicle_positions = serializers.BooleanField(
        help_text='Whether the planner receives this operator\'s vehicle positions at all.'
    )
    trips_under_way = serializers.IntegerField(help_text='Trips running now (predicted times).')
    trips_with_position = serializers.IntegerField(
        help_text='Of those, how many a vehicle reports its position for.'
    )
    coverage = serializers.FloatField(
        allow_null=True,
        help_text='trips_with_position / trips_under_way; null when none run or no positions.',
    )


class CoverageParams(serializers.Serializer):
    agencies = id_list(help_text='Operator IDs.')
    organizations = organization_list(help_text='ODT organization IDs (all their operators).')

    def validate(self, attrs):
        agency_ids = agency_filter(attrs.get('agencies'), attrs.get('organizations'))
        if not agency_ids:
            # Counting an operator's trips is heavy: only for the operators asked about.
            raise serializers.ValidationError(
                {'agencies': ['Name the operators.']}, code='required'
            )
        if len(agency_ids) > MAX_IDS:
            raise serializers.ValidationError(
                {'organizations': ['Too many operators.']}, code='max_length'
            )
        attrs['agency_ids'] = agency_ids
        return attrs


class AlertParams(serializers.Serializer):
    routes = id_list(help_text='About these lines (or their trips).')
    stops = id_list(help_text='About these stops.')
    agencies = id_list(help_text='About these operators or their lines.')
    organizations = organization_list(help_text='About the operators of these ODT organizations.')

    def validate(self, attrs):
        attrs['agency_ids'] = agency_filter(attrs.get('agencies'), attrs.get('organizations'))
        return attrs


class BlockParams(serializers.Serializer):
    id = FeedScopedIdField(help_text='A trip of the block, e.g. from a vehicle or a departure.')
    date = serializers.DateField(
        required=False, help_text='Service date; default: the day of the run under way or next.'
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class ScheduleView(PlannerPublicView):
    limit_group = 'realtime'
    uses_otp = True

    def ask_otp(self, document: str, variables: dict) -> dict:
        return self.otp_query(document, variables, timeout=SCHEDULE_TIMEOUT_SECONDS)

    def service_date(self, params: dict):
        """The `date` parameter, or the day of the trip's run under way or next."""
        if params.get('date'):
            return params['date']
        data = self.ask_otp(
            schedules.CURRENT_RUN_DOCUMENT,
            {'id': params['id'], 'yesterday': (schedules.today() - timedelta(days=1)).isoformat()},
        )
        return schedules.current_service_date(data, timezone.now())

    def check_service_period(self, data: dict, day) -> None:
        period = schedules.service_period(data)
        if period and not period[0] <= day <= period[1]:
            raise PlannerError(
                'planner.date_outside_service_period',
                404,
                details={'first_date': period[0].isoformat(), 'last_date': period[1].isoformat()},
            )


class DeparturesView(ScheduleView):
    """Next departures from one or more stops, with realtime predictions and cancellations."""

    @extend_schema(
        parameters=[DeparturesParams], responses=DeparturesSerializer, summary='Departures'
    )
    def get(self, request):
        params = self.validated(DeparturesParams)
        routes = set(params.get('routes') or [])
        data = self.ask_otp(
            schedules.DEPARTURES_DOCUMENT,
            schedules.departures_variables(
                params['id'],
                params.get('time') or timezone.now(),
                params['window'],
                params['limit'],
                by_variant=bool(routes),
            ),
        )
        rows = schedules.departures(data, params['limit'], routes)
        if rows is None:
            raise PlannerError('planner.stop_not_found', 404)
        return Response(DeparturesSerializer({'departures': rows}).data)


class StopTimetableView(ScheduleView):
    """Every scheduled departure of a stop on one day, by line and headsign."""

    @extend_schema(
        parameters=[StopTimetableParams],
        responses=StopTimetableSerializer,
        summary='Stop timetable for a day',
    )
    def get(self, request):
        params = self.validated(StopTimetableParams)
        day = params.get('date') or schedules.today()
        data = self.ask_otp(
            schedules.STOP_TIMETABLE_DOCUMENT,
            {'id': params['id'], 'date': day.strftime('%Y%m%d')},
        )
        timetable = schedules.stop_timetable(data, set(params.get('routes') or []))
        if timetable is None:
            raise PlannerError('planner.stop_not_found', 404)
        self.check_service_period(data, day)
        return Response(StopTimetableSerializer({**timetable, 'date': day}).data)


class RouteView(ScheduleView):
    """A line with its variants (stop sequences) and, on request, their paths."""

    @extend_schema(parameters=[RouteParams], responses=RouteDetailSerializer, summary='A line')
    def get(self, request):
        params = self.validated(RouteParams)
        data = self.ask_otp(
            schedules.ROUTE_DOCUMENT, {'id': params['id'], 'withGeometry': params['geometry']}
        )
        route = schedules.route_detail(data)
        if route is None:
            raise PlannerError('planner.route_not_found', 404)
        return Response(RouteDetailSerializer(route).data)


class TripView(ScheduleView):
    """One trip on its service date: every stop with scheduled and predicted times."""

    @extend_schema(parameters=[TripParams], responses=TripDetailSerializer, summary='A trip')
    def get(self, request):
        params = self.validated(TripParams)
        day = self.service_date(params)
        data = self.ask_otp(
            schedules.TRIP_DOCUMENT,
            {'id': params['id'], 'date': day.isoformat(), 'withGeometry': params['geometry']},
        )
        trip = schedules.trip_detail(data)
        if trip is None:
            raise PlannerError('planner.trip_not_found', 404)
        self.check_service_period(data, day)
        if trip['calls'] is None:
            raise PlannerError('planner.trip_not_on_date', 404)
        return Response(TripDetailSerializer(trip).data)


class BlockView(ScheduleView):
    """Every trip of the vehicle's block (brigade) on a day, with the one under way marked."""

    @extend_schema(parameters=[BlockParams], responses=BlockSerializer, summary='A block (brigade)')
    def get(self, request):
        params = self.validated(BlockParams)
        member = IndexedBlockTrip.objects.filter(trip_id=params['id']).first()
        if member is None:
            raise PlannerError('planner.block_not_available', 404, details={'reason': 'no_block'})
        trip_ids = list(
            IndexedBlockTrip.objects.filter(feed_id=member.feed_id, block_id=member.block_id)
            .order_by('trip_id')
            .values_list('trip_id', flat=True)[: schedules.MAX_BLOCK_TRIPS + 1]
        )
        if len(trip_ids) > schedules.MAX_BLOCK_TRIPS:
            # Feeds that reuse one block ID for a whole line are no use here.
            raise PlannerError(
                'planner.block_not_available', 404, details={'reason': 'too_many_trips'}
            )
        day = self.service_date(params)
        data = self.ask_otp(
            schedules.block_document(len(trip_ids)), schedules.block_variables(trip_ids, day)
        )
        self.check_service_period(data, day)
        trips = schedules.block_trips(data, len(trip_ids), timezone.now())
        if not trips:
            raise PlannerError('planner.trip_not_on_date', 404)
        return Response(
            BlockSerializer(
                {
                    'feed_id': member.feed_id,
                    'block_id': member.block_id,
                    'service_date': day,
                    'trips': trips,
                }
            ).data
        )


@extend_schema(parameters=[VehicleParams], summary='Vehicles')
class VehicleListView(LookupListView):
    """Vehicles reporting their position now, matched to a trip; refreshed every 10 s."""

    params_class = VehicleParams
    serializer_class = VehicleSerializer
    pagination_class = MapPagination
    limit_group = 'realtime'
    uses_otp = True

    def get_queryset(self):
        params = self.params()
        try:
            rows = vehicles.snapshot(
                lambda document, variables: self.otp_query(
                    document, variables, timeout=SCHEDULE_TIMEOUT_SECONDS
                )
            )
        except otp_runtime.OtpBusy:
            raise PlannerError(
                'planner.busy', 503, retry_after=OTP_BUSY_RETRY_AFTER_SECONDS
            ) from None
        return vehicles.matching(
            rows,
            bbox=params.get('bbox'),
            modes=params.get('modes'),
            agency_ids=params['agency_ids'],
            route_ids=set(params.get('routes') or []),
        )


@extend_schema(parameters=[AlertParams], summary='Service alerts')
class AlertListView(LookupListView):
    """Service alerts from the operators, those in force first; filtered by what they affect."""

    params_class = AlertParams
    serializer_class = ServiceAlertSerializer
    limit_group = 'realtime'
    uses_otp = True

    def get_queryset(self):
        params = self.params()
        route_ids = set(params.get('routes') or [])
        stop_ids = set(params.get('stops') or [])
        agency_ids = params['agency_ids']
        if agency_ids:
            # An alert about a line counts for its operator.
            route_ids |= set(
                IndexedRoute.objects.filter(agency__agency_id__in=agency_ids).values_list(
                    'route_id', flat=True
                )
            )
        requested = route_ids | stop_ids | set(agency_ids or [])
        feeds = sorted({item.split(':', 1)[0] for item in requested}) or None
        data = self.otp_query(
            alerts.ALERTS_DOCUMENT, {'feeds': feeds}, timeout=SCHEDULE_TIMEOUT_SECONDS
        )
        return alerts.matching(alerts.alerts(data, timezone.now()), route_ids, stop_ids, agency_ids)


@extend_schema(parameters=[RentalParams], summary='Bike and scooter sharing')
class RentalListView(LookupListView):
    """Sharing stations and free vehicles in an area, with what can be rented there now."""

    params_class = RentalParams
    serializer_class = RentalPlaceSerializer
    pagination_class = MapPagination
    limit_group = 'realtime'
    uses_otp = True

    def get_queryset(self):
        params = self.params()
        data = self.otp_query(
            rentals.RENTALS_DOCUMENT,
            rentals.bbox_variables(params['bbox']),
            timeout=SCHEDULE_TIMEOUT_SECONDS,
        )
        return rentals.matching(
            rentals.rentals(data), set(params.get('networks') or []), params.get('form_factors')
        )


@extend_schema(parameters=[CoverageParams], summary='Position coverage of operators')
class VehicleCoverageView(LookupListView):
    """How many of the chosen operators' trips under way now report a vehicle position.

    Trips counted as under way are refreshed once a minute; vehicles as in vehicles/.
    """

    params_class = CoverageParams
    serializer_class = CoverageSerializer
    limit_group = 'realtime'
    uses_otp = True

    def get_queryset(self):
        agency_ids = self.params()['agency_ids']
        graph = GraphBuild.objects.filter(is_active=True).values('id', 'feed_results').first()
        if graph is None:
            return []
        # Feeds OTP polls vehicle positions for: the others report none by design
        # (no GTFS-RT positions, realtime off, or served from a stale snapshot).
        with_positions = {
            result['feed_id']
            for result in graph['feed_results']
            if 'vehicle-positions' in (result.get('realtime_polled') or [])
        }

        query = partial(self.otp_query, timeout=SCHEDULE_TIMEOUT_SECONDS)
        # OTP fails on the start or end of a few trips of some operators (Warsaw on
        # 2026-10-16: 4 of 1,927 patterns came back null); the other patterns still count.
        running_query = partial(query, partial=True)

        try:
            reporting = {row['trip_id'] for row in vehicles.snapshot(query)}
        except otp_runtime.OtpBusy:
            raise PlannerError(
                'planner.busy', 503, retry_after=OTP_BUSY_RETRY_AFTER_SECONDS
            ) from None
        rows = []
        for agency_id in agency_ids:
            running = vehicles.running_trips(agency_id, graph['id'], running_query)
            if running is None:
                continue  # not an operator of the served graph
            positions = agency_id.split(':', 1)[0] in with_positions
            under_way = len(running['trips'])
            with_position = len(reporting.intersection(running['trips']))
            rows.append(
                {
                    'agency': running['agency'],
                    'vehicle_positions': positions,
                    'trips_under_way': under_way,
                    'trips_with_position': with_position,
                    'coverage': (
                        round(with_position / under_way, 4) if positions and under_way else None
                    ),
                }
            )
        return rows
