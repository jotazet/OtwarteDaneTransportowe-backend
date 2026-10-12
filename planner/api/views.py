from django.core.cache import cache
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from data_manager.models import FeedSubmission, completed_submission_ids
from OtwarteDaneTransportowe.auth_roles import RequiresAdminGroup
from planner import otp_runtime, services
from planner.api.serializers import (
    BuildScheduleSerializer,
    FeedCandidateSerializer,
    GraphBuildDetailSerializer,
    GraphBuildSerializer,
    PlannerFeedSerializer,
)
from planner.models import GraphBuild, PlannerFeed

OTP_HEALTH_CACHE_KEY = 'planner:otp-healthy'
OTP_HEALTH_CACHE_SECONDS = 15


def _error(code: str, http_status: int) -> Response:
    return Response({'code': code, 'details': {}}, status=http_status)


def _queued(request_build) -> Response:
    try:
        build = request_build()
    except services.BuildInProgress:
        return _error('planner.build_in_progress', status.HTTP_409_CONFLICT)
    except services.BuildNotActivatable:
        return _error('planner.build_not_activatable', status.HTTP_409_CONFLICT)
    return Response(GraphBuildSerializer(build).data, status=status.HTTP_202_ACCEPTED)


class PlannerFeedViewSet(viewsets.ModelViewSet):
    """Feeds included in the planner graph."""

    permission_classes = [RequiresAdminGroup]
    serializer_class = PlannerFeedSerializer
    queryset = PlannerFeed.objects.select_related('submission__transport_organization')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        active = GraphBuild.objects.filter(is_active=True).only('feed_results').first()
        context['active_results'] = {
            result['planner_feed']: result for result in (active.feed_results if active else [])
        }
        return context


class FeedCandidateListView(generics.ListAPIView):
    """Published GTFS feeds the admin can add to the planner."""

    permission_classes = [RequiresAdminGroup]
    serializer_class = FeedCandidateSerializer

    def get_queryset(self):
        return (
            FeedSubmission.objects.filter(data_type='gtfs', id__in=completed_submission_ids())
            .select_related('transport_organization', 'planner_feed')
            .order_by(
                'transport_organization__region',
                'transport_organization__transport_organization',
                'id',
            )
        )


class GraphBuildViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [RequiresAdminGroup]
    queryset = GraphBuild.objects.select_related('requested_by')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return GraphBuildDetailSerializer
        return GraphBuildSerializer

    def create(self, request):
        """Build now, outside the schedule."""
        return _queued(lambda: services.request_build(GraphBuild.TRIGGER_MANUAL, request.user))

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Serve this earlier build's graph again (rollback)."""
        target = self.get_object()
        return _queued(lambda: services.request_rollback(target, request.user))


class BuildScheduleView(APIView):
    permission_classes = [RequiresAdminGroup]

    def get(self, request):
        return Response(services.get_build_schedule())

    def put(self, request):
        serializer = BuildScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(services.set_build_schedule(**serializer.validated_data))


class PlannerStatusView(APIView):
    """What is served now, whether a build is running and whether OTP answers."""

    permission_classes = [RequiresAdminGroup]

    def get(self, request):
        active = GraphBuild.objects.filter(is_active=True).select_related('requested_by').first()
        running = (
            GraphBuild.objects.filter(status__in=GraphBuild.ACTIVE_STATUSES)
            .select_related('requested_by')
            .first()
        )
        healthy = cache.get(OTP_HEALTH_CACHE_KEY)
        if healthy is None:
            healthy = otp_runtime.is_healthy()
            cache.set(OTP_HEALTH_CACHE_KEY, healthy, OTP_HEALTH_CACHE_SECONDS)
        return Response(
            {
                'active_build': GraphBuildSerializer(active).data if active else None,
                'running_build': GraphBuildSerializer(running).data if running else None,
                'maintenance': services.maintenance_active(),
                'otp_healthy': healthy,
            }
        )
