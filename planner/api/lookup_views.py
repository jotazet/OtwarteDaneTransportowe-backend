"""Stop, line and operator lookups of the public planner API (docs/api/planner.md).

Answered from the search index in Postgres, so they keep working while OTP is
stopped for a graph build.
"""

from django.db.models import Count
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import generics, serializers
from rest_framework.response import Response

from OtwarteDaneTransportowe.pagination import DefaultPageNumberPagination
from planner import search
from planner.access import PlannerError, PlannerPublicView
from planner.api.params import (
    BboxField,
    FeedScopedIdField,
    OperatorFilter,
    PositionField,
    SearchTextField,
    agency_filter,
    id_list,
    organization_list,
)
from planner.models import IndexedRoute, IndexedStop

# 0.5 degrees is ~55 x 35 km in Poland, more than a city: a map shows stops
# only when zoomed in further.
MAX_BBOX_DEGREES = 0.5
NEARBY_DEFAULT_RADIUS_M = 500
NEARBY_MAX_RADIUS_M = 2000
# Lines of a place: answered from Postgres, so it takes more poles than
# departures (which asks OTP).
ROUTE_STOPS_MAX = 50


class SearchPagination(DefaultPageNumberPagination):
    # Each result carries its poles; autocomplete shows a handful.
    page_size = 10
    max_page_size = 50


class MapPagination(DefaultPageNumberPagination):
    # A map needs every stop of its viewport in one answer; the bbox is capped instead.
    page_size = 500
    max_page_size = 2000


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class OptionalText(serializers.CharField):
    """The index stores missing text as ''; the API returns null."""

    def to_representation(self, value):
        return value or None


class AgencyRefSerializer(serializers.Serializer):
    id = serializers.CharField(source='agency_id')
    name = serializers.CharField()


class RouteSerializer(serializers.Serializer):
    id = serializers.CharField(source='route_id')
    short_name = OptionalText()
    long_name = OptionalText()
    mode = serializers.CharField()
    color = OptionalText(help_text='Hex without `#`, as in the feed.')
    text_color = OptionalText()
    agency = AgencyRefSerializer()


class StopSerializer(serializers.Serializer):
    id = serializers.CharField(source='stop_id')
    name = serializers.CharField()
    code = OptionalText(help_text='Code shown to passengers, if the feed has one.')
    platform = OptionalText()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    mode = OptionalText()
    station_id = OptionalText(help_text='GTFS parent station, if any.')
    heading = serializers.IntegerField(
        allow_null=True,
        help_text='Which way vehicles leave the pole: degrees clockwise from north.',
    )


class StopGroupSerializer(serializers.Serializer):
    """A place: every pole with the same name close together that matches the filters."""

    name = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    modes = serializers.SerializerMethodField(help_text='Modes of the poles in `stops`.')
    distance_m = serializers.SerializerMethodField(help_text='From `near`, if given.')
    stops = StopSerializer(many=True)

    def get_modes(self, group) -> list[str]:
        return sorted({stop.mode for stop in group.stops.all() if stop.mode})

    def get_distance_m(self, group) -> int | None:
        distance = getattr(group, 'distance', None)
        return round(distance) if distance is not None else None


class StopGroupRefSerializer(serializers.Serializer):
    name = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    stops = StopSerializer(many=True)


class StopDetailSerializer(StopSerializer):
    """A stop with its lines and the other poles of the same place."""

    routes = serializers.SerializerMethodField()
    group = StopGroupRefSerializer(help_text='Every pole with this name nearby.')

    @extend_schema_field(RouteSerializer(many=True))
    def get_routes(self, stop):
        routes = self.context['routes']
        found = [routes[route_id] for route_id in stop.route_ids if route_id in routes]
        found.sort(key=lambda route: (route.mode, route.sort_key))
        return RouteSerializer(found, many=True).data


class OrganizationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField(source='transport_organization')
    region = serializers.CharField()


class AgencySerializer(serializers.Serializer):
    id = serializers.CharField(source='agency_id')
    name = serializers.CharField()
    url = OptionalText()
    feed_id = serializers.CharField()
    organization = OrganizationSerializer(allow_null=True, help_text='ODT organization.')
    route_count = serializers.IntegerField()


def routes_by_id(route_ids) -> dict:
    return {
        route.route_id: route
        for route in IndexedRoute.objects.filter(route_id__in=set(route_ids)).select_related(
            'agency'
        )
    }


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class StopSearchParams(OperatorFilter):
    # Three letters: shorter fragments match too much to rank, and cannot use
    # the trigram index.
    q = SearchTextField(3, help_text='Stop name, at least 3 letters; any case.')
    near = PositionField(required=False, help_text='`lat,lon`: rank stops near it higher.')


class NearbyParams(OperatorFilter):
    near = PositionField(help_text='`lat,lon`.')
    radius = serializers.IntegerField(
        min_value=1, max_value=NEARBY_MAX_RADIUS_M, default=NEARBY_DEFAULT_RADIUS_M
    )


class BboxParams(OperatorFilter):
    bbox = BboxField(
        MAX_BBOX_DEGREES, help_text='`min_lon,min_lat,max_lon,max_lat`, at most 0.5° a side.'
    )


class RouteSearchParams(OperatorFilter):
    q = SearchTextField(1, required=False, help_text='Line number or name, e.g. `70`.')
    near = PositionField(required=False, help_text='`lat,lon`: lines near it first.')
    stops = id_list(
        ROUTE_STOPS_MAX,
        help_text=f'Stop IDs (repeat, up to {ROUTE_STOPS_MAX}): lines calling at any of them.',
    )


class AgencyParams(serializers.Serializer):
    organizations = organization_list(help_text='ODT organization IDs.')

    def validate(self, attrs):
        attrs['agency_ids'] = agency_filter(None, attrs.get('organizations'))
        return attrs


class StopParams(serializers.Serializer):
    id = FeedScopedIdField(help_text='Stop ID, e.g. `krk:2047470`.')


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class LookupListView(PlannerPublicView, generics.ListAPIView):
    """A paginated lookup: parameters are validated once, then filter the queryset."""

    params_class = None
    filter_backends = []

    def params(self) -> dict:
        if not hasattr(self, '_params'):
            self._params = self.validated(self.params_class)
        return self._params


class GroupListView(LookupListView):
    serializer_class = StopGroupSerializer
    pagination_class = SearchPagination


@extend_schema(parameters=[StopSearchParams], summary='Search stops by name')
class StopSearchView(GroupListView):
    """Stops whose name matches `q`, best first, one result per place.

    Poles with the same name close together are one result with all of them in
    `stops`; with filters, only the poles that match. Polish letters are
    optional: `lodz` finds `Łódź`. Lines of a place: `routes/?stops=`.
    """

    params_class = StopSearchParams

    def get_queryset(self):
        params = self.params()
        return search.search_stop_groups(
            params['q'], params.get('near'), params.get('modes'), params['agency_ids']
        )


@extend_schema(parameters=[NearbyParams], summary='Stops near a point')
class StopsNearbyView(GroupListView):
    """Places with stops within `radius` metres of `near`, nearest first."""

    params_class = NearbyParams

    def get_queryset(self):
        params = self.params()
        return search.nearby_stop_groups(
            params['near'], params['radius'], params.get('modes'), params['agency_ids']
        )


@extend_schema(parameters=[BboxParams], summary='Stops in a map area')
class StopsInAreaView(LookupListView):
    """Every stop (single pole) in `bbox`, for drawing them on a map."""

    params_class = BboxParams
    serializer_class = StopSerializer
    pagination_class = MapPagination

    def get_queryset(self):
        params = self.params()
        return search.stops_in_bbox(params['bbox'], params.get('modes'), params['agency_ids'])


@extend_schema(parameters=[RouteSearchParams], summary='Search lines')
class RouteSearchView(LookupListView):
    """Lines by number or name; without `q`, every line matching the filters.

    With `stops`, only lines calling at any of those stops.
    """

    params_class = RouteSearchParams
    serializer_class = RouteSerializer

    def get_queryset(self):
        params = self.params()
        return search.search_routes(
            params.get('q'),
            params.get('near'),
            params.get('modes'),
            params['agency_ids'],
            params.get('stops'),
        )


@extend_schema(parameters=[AgencyParams], summary='Operators')
class AgencyListView(LookupListView):
    """Operators in the planner, with the ODT organization that publishes their data."""

    params_class = AgencyParams
    serializer_class = AgencySerializer

    def get_queryset(self):
        return search.agencies(self.params()['agency_ids']).annotate(route_count=Count('routes'))


class StopView(PlannerPublicView):
    """One stop with its lines and the other poles of the same place."""

    @extend_schema(parameters=[StopParams], responses=StopDetailSerializer, summary='A stop')
    def get(self, request):
        stop = (
            IndexedStop.objects.select_related('group')
            .prefetch_related('group__stops')
            .filter(stop_id=self.validated(StopParams)['id'])
            .first()
        )
        if stop is None:
            raise PlannerError('planner.stop_not_found', 404)
        context = {'routes': routes_by_id(stop.route_ids)}
        return Response(StopDetailSerializer(stop, context=context).data)
