"""Stop, line and operator lookups over the search index (planner/search_index.py).

Every function returns a queryset, so views paginate it. Names are compared
after search_index.search_text(), so "lodz" finds "Łódź".
"""

import math

from django.contrib.postgres.expressions import ArraySubquery
from django.contrib.postgres.search import TrigramWordSimilarity
from django.db.models import (
    Case,
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
    Func,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Least, Power, Sqrt

from planner.models import IndexedAgency, IndexedRoute, IndexedStop, IndexedStopGroup
from planner.search_index import METERS_PER_DEGREE, search_text

# Ranking weights: name similarity is 0..1; a name starting with the query, a
# busy stop (many lines) and nearness to `near` lift a result.
PREFIX_BONUS = 0.3
BUSY_BONUS_PER_ROUTE = 0.005  # up to 20 lines: +0.1
NEAR_BONUS = 0.4  # halves every 5 km
NEAR_HALF_DISTANCE_M = 5000


def organization_agencies(organization_ids: list[int]) -> tuple[list[str], list[int]]:
    """(agency IDs of the organizations, organizations without any agency in the planner)."""
    rows = IndexedAgency.objects.filter(organization_id__in=organization_ids).values_list(
        'organization_id', 'agency_id'
    )
    agency_ids = sorted({agency_id for _, agency_id in rows})
    found = {organization_id for organization_id, _ in rows}
    return agency_ids, [org for org in organization_ids if org not in found]


def search_stop_groups(text, near=None, modes=(), agency_ids=None) -> QuerySet:
    query = search_text(text)
    groups = IndexedStopGroup.objects.filter(
        Q(name_search__trigram_word_similar=query)
        | Q(name_search__startswith=query)
        | Q(name_search__contains=f' {query}')
    )
    score = (
        TrigramWordSimilarity(query, 'name_search')
        + Case(When(name_search__startswith=query, then=Value(PREFIX_BONUS)), default=Value(0.0))
        + _float(Least(F('route_count'), Value(20)) * Value(BUSY_BONUS_PER_ROUTE))
    )
    groups = _filter_groups(groups, modes, agency_ids)
    if near:
        groups = groups.annotate(distance=_distance(*near))
        score = score + _float(
            Value(NEAR_BONUS) / (Value(1.0) + F('distance') / Value(NEAR_HALF_DISTANCE_M))
        )
    return groups.annotate(score=_float(score)).order_by('-score', '-route_count', 'name', 'id')


def nearby_stop_groups(near, radius_m, modes=(), agency_ids=None) -> QuerySet:
    lat, lon = near
    groups = _filter_groups(
        _around(IndexedStopGroup.objects.all(), lat, lon, radius_m), modes, agency_ids
    )
    return (
        groups.annotate(distance=_distance(lat, lon))
        .filter(distance__lte=radius_m)
        .order_by('distance', 'id')
    )


def stops_in_bbox(bbox, modes=(), agency_ids=None) -> QuerySet:
    min_lon, min_lat, max_lon, max_lat = bbox
    stops = IndexedStop.objects.filter(
        lat__gte=min_lat, lat__lte=max_lat, lon__gte=min_lon, lon__lte=max_lon
    )
    return _filter_stops(stops, modes, agency_ids).order_by('stop_id')


def search_routes(text=None, near=None, modes=(), agency_ids=None, stop_ids=None) -> QuerySet:
    routes = IndexedRoute.objects.select_related('agency')
    if stop_ids:
        calling = IndexedStop.objects.filter(stop_id__in=stop_ids).annotate(
            calling_route_id=Func('route_ids', function='unnest', output_field=TextField())
        )
        routes = routes.filter(route_id__in=calling.values('calling_route_id'))
    if modes:
        routes = routes.filter(mode__in=modes)
    if agency_ids is not None:
        routes = routes.filter(agency__agency_id__in=agency_ids)
    ordering = ['sort_key', 'route_id']
    if near:
        routes = routes.annotate(distance=_distance(*near))
        ordering.insert(0, F('distance').asc(nulls_last=True))
    if text:
        query = search_text(text)
        routes = routes.filter(
            Q(short_name_search=query)
            | Q(short_name_search__startswith=query)
            | Q(name_search__trigram_word_similar=query)
        ).annotate(
            score=_float(
                Case(
                    When(short_name_search=query, then=Value(3.0)),
                    When(short_name_search__startswith=query, then=Value(2.0)),
                    default=Value(0.0),
                )
                + TrigramWordSimilarity(query, 'name_search')
            )
        )
        ordering.insert(0, '-score')
    return routes.order_by(*ordering)


def agencies(agency_ids=None) -> QuerySet:
    rows = IndexedAgency.objects.select_related('organization')
    if agency_ids is not None:
        rows = rows.filter(agency_id__in=agency_ids)
    return rows.order_by('name', 'agency_id')


def _filter_stops(stops, modes, agency_ids):
    if modes:
        stops = stops.filter(mode__in=modes)
    if agency_ids is not None:
        # Per pole, not per place: a place can mix operators.
        agency_routes = IndexedRoute.objects.filter(agency__agency_id__in=agency_ids)
        stops = stops.filter(route_ids__overlap=ArraySubquery(agency_routes.values('route_id')))
    return stops


def _filter_groups(groups, modes, agency_ids):
    """Places with a pole that matches the filters, each with only those poles."""
    if not modes and agency_ids is None:
        return groups.prefetch_related('stops')
    stops = _filter_stops(IndexedStop.objects.all(), modes, agency_ids)
    return groups.filter(Exists(stops.filter(group=OuterRef('pk')))).prefetch_related(
        Prefetch('stops', queryset=stops)
    )


def _around(queryset, lat, lon, radius_m):
    """Cheap bounding-box prefilter (uses the lat/lon index) before exact distances."""
    dlat = radius_m / METERS_PER_DEGREE
    dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
    return queryset.filter(
        lat__gte=lat - dlat, lat__lte=lat + dlat, lon__gte=lon - dlon, lon__lte=lon + dlon
    )


def _distance(lat: float, lon: float):
    """Metres from (lat, lon), equirectangular like search_index.distance_m."""
    x_scale = METERS_PER_DEGREE * math.cos(math.radians(lat))
    return _float(
        Sqrt(
            Power((F('lon') - Value(lon)) * Value(x_scale), 2)
            + Power((F('lat') - Value(lat)) * Value(METERS_PER_DEGREE), 2)
        )
    )


def _float(expression):
    return ExpressionWrapper(expression, output_field=FloatField())
