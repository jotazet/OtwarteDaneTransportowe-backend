"""Public planner API (/api/planner/v1/). Documented in docs/api/planner.md."""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from planner import services
from planner.access import PlannerPublicView, planner_settings
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
