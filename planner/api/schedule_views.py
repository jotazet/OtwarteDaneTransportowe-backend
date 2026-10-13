"""Departures, stop timetables, lines and trips of the public planner API (docs/api/planner.md).

Answered by OTP, so with realtime data where operators publish it, and
unavailable while OTP is stopped for a graph build.
"""

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from planner import schedules
from planner.access import PlannerError, PlannerPublicView
from planner.api.params import MAX_IDS, FeedScopedIdField, InstantField, QueryBoolean, id_list
from planner.api.public_views import AgencySummarySerializer

SCHEDULE_TIMEOUT_SECONDS = 10
STATUSES = ['scheduled', 'updated', 'canceled', 'added', 'modified']


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
        required=False, help_text='Service date (`service_date`); default today in Poland.'
    )
    geometry = QueryBoolean(default=False, help_text="Return the trip's path.")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class ScheduleView(PlannerPublicView):
    limit_group = 'realtime'
    uses_otp = True

    def ask_otp(self, document: str, variables: dict) -> dict:
        return self.otp_query(document, variables, timeout=SCHEDULE_TIMEOUT_SECONDS)

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
        day = params.get('date') or schedules.today()
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
