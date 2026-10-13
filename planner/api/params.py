"""Query parameters shared by the public planner endpoints (docs/api/planner.md)."""

from django.core.validators import RegexValidator
from rest_framework import serializers

from planner import planning, search
from planner.search_index import search_text

MAX_IDS = 20


class QueryBoolean(serializers.BooleanField):
    """A query-string flag that falls back to its default when absent (not to False)."""

    default_empty_html = serializers.empty


class FeedScopedIdField(serializers.CharField):
    """`<feed_id>:<id in the feed>`, as OTP expects every stop, route and agency ID."""

    def __init__(self, **kwargs):
        super().__init__(max_length=128, **kwargs)
        self.validators.append(
            RegexValidator(planning.FEED_SCOPED_ID, 'Not a feed-scoped ID.', 'planner.invalid_id')
        )


def id_list(max_length=MAX_IDS, **kwargs):
    return serializers.ListField(
        child=FeedScopedIdField(), max_length=max_length, required=False, **kwargs
    )


def organization_list(**kwargs):
    return serializers.ListField(
        child=serializers.IntegerField(min_value=1), max_length=MAX_IDS, required=False, **kwargs
    )


class PositionField(serializers.CharField):
    """`lat,lon` in WGS 84, as a (lat, lon) tuple."""

    def to_internal_value(self, data):
        location = planning.parse_place(super().to_internal_value(data)).get('coordinate')
        if not location or not (
            -90 <= location['latitude'] <= 90 and -180 <= location['longitude'] <= 180
        ):
            raise serializers.ValidationError('Not lat,lon.', code='planner.invalid_location')
        return location['latitude'], location['longitude']


class BboxField(serializers.CharField):
    """`min_lon,min_lat,max_lon,max_lat` (GeoJSON order), at most ``max_degrees`` a side."""

    def __init__(self, max_degrees, **kwargs):
        super().__init__(**kwargs)
        self.max_degrees = max_degrees

    def to_internal_value(self, data):
        try:
            min_lon, min_lat, max_lon, max_lat = (
                float(part) for part in super().to_internal_value(data).split(',')
            )
        except ValueError:
            raise serializers.ValidationError(
                'Not min_lon,min_lat,max_lon,max_lat.', code='planner.invalid_bbox'
            ) from None
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise serializers.ValidationError('Empty or inverted.', code='planner.invalid_bbox')
        if max_lon - min_lon > self.max_degrees or max_lat - min_lat > self.max_degrees:
            raise serializers.ValidationError('Too large.', code='planner.bbox_too_large')
        return min_lon, min_lat, max_lon, max_lat


class SearchTextField(serializers.CharField):
    """Free text compared after search_text(); at least ``min_letters`` letters or digits."""

    def __init__(self, min_letters, **kwargs):
        super().__init__(max_length=100, **kwargs)
        self.min_letters = min_letters

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if len(search_text(value).replace(' ', '')) < self.min_letters:
            raise serializers.ValidationError('Too short.', code='min_length')
        return value


class OperatorFilter(serializers.Serializer):
    """`agencies` and `organizations` (ODT organization IDs), merged into ``agency_ids``.

    ``agency_ids`` is None when neither is given (no filter).
    """

    modes = serializers.ListField(
        child=serializers.ChoiceField(choices=list(planning.TRANSIT_MODES)),
        required=False,
        help_text='Repeat the parameter.',
    )
    agencies = id_list(help_text='Operator IDs (`<feed_id>:<agency_id>`).')
    organizations = organization_list(help_text='ODT organization IDs (see `agencies/`).')

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs['agency_ids'] = agency_filter(attrs.get('agencies'), attrs.get('organizations'))
        return attrs


def agency_filter(agencies, organizations, field='organizations') -> list[str] | None:
    """Agency IDs from explicit IDs plus every agency of the organizations; None for no filter."""
    if not agencies and not organizations:
        return None
    agency_ids = set(agencies or [])
    if organizations:
        found, unknown = search.organization_agencies(organizations)
        if unknown:
            raise serializers.ValidationError(
                {field: 'Not in the planner.'}, code='planner.organization_not_found'
            )
        agency_ids.update(found)
    return sorted(agency_ids)
