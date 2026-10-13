import secrets
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from data_manager.models import (
    FeedSubmission,
    RealtimeSubmission,
    completed_realtime_submission_ids,
    completed_submission_ids,
)
from OtwarteDaneTransportowe.auth_roles import RequiresAdminGroup
from planner import access, otp_runtime, request_logs, services
from planner.api.serializers import (
    ApiKeySerializer,
    BuildScheduleSerializer,
    DailyStatsFilterSerializer,
    FeedCandidateSerializer,
    GraphBuildDetailSerializer,
    GraphBuildSerializer,
    IpBanSerializer,
    OsmDownloadRequestSerializer,
    OsmDownloadSerializer,
    PlannerFeedSerializer,
    PlannerRentalSourceSerializer,
    PlannerRequestLogSerializer,
    PlannerSettingsSerializer,
    RentalCandidateSerializer,
    RequestLogFilterSerializer,
    StatsFilterSerializer,
    TimeseriesFilterSerializer,
    TopClientsFilterSerializer,
)
from planner.models import (
    ApiKey,
    GraphBuild,
    IpBan,
    OsmDownload,
    PlannerFeed,
    PlannerRentalSource,
    PlannerSettings,
)

OTP_HEALTH_CACHE_KEY = 'planner:otp-healthy'
OTP_HEALTH_CACHE_SECONDS = 15


def _error(code: str, http_status: int) -> Response:
    return Response({'code': code, 'details': {}}, status=http_status)


def _queued(request_job, serializer_class=None) -> Response:
    """202 with the queued build or download, or 409 while another job is under way."""
    try:
        job = request_job()
    except services.BuildInProgress:
        return _error('planner.build_in_progress', status.HTTP_409_CONFLICT)
    except services.OsmDownloadInProgress:
        return _error('planner.osm_download_in_progress', status.HTTP_409_CONFLICT)
    except services.BuildNotActivatable:
        return _error('planner.build_not_activatable', status.HTTP_409_CONFLICT)
    serializer_class = serializer_class or GraphBuildSerializer
    return Response(serializer_class(job).data, status=status.HTTP_202_ACCEPTED)


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


class PlannerRentalSourceViewSet(viewsets.ModelViewSet):
    """Bike and scooter sharing (GBFS) feeds the planner routes with."""

    permission_classes = [RequiresAdminGroup]
    serializer_class = PlannerRentalSourceSerializer
    queryset = PlannerRentalSource.objects.select_related('submission__transport_organization')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        active = GraphBuild.objects.filter(is_active=True).only('rental_results').first()
        context['active_results'] = {
            result['rental_source']: result for result in (active.rental_results if active else [])
        }
        return context


class RentalCandidateListView(generics.ListAPIView):
    """Published GBFS feeds the admin can add to the planner."""

    permission_classes = [RequiresAdminGroup]
    serializer_class = RentalCandidateSerializer

    def get_queryset(self):
        return (
            RealtimeSubmission.objects.filter(
                protocol=RealtimeSubmission.PROTOCOL_GBFS,
                id__in=completed_realtime_submission_ids(),
            )
            .select_related('transport_organization', 'planner_rental_source')
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


class OsmDownloadView(generics.ListAPIView):
    """Replacements of the OSM extract by a URL; the newest succeeded one is in use."""

    permission_classes = [RequiresAdminGroup]
    serializer_class = OsmDownloadSerializer
    queryset = OsmDownload.objects.select_related('requested_by')

    def post(self, request):
        params = OsmDownloadRequestSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return _queued(
            lambda: services.request_osm_download(params.validated_data['url'], request.user),
            OsmDownloadSerializer,
        )


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


# ---------------------------------------------------------------------------
# Public API access: settings, keys, bans, request log, statistics
# ---------------------------------------------------------------------------


class PlannerSettingsView(APIView):
    """Kill switch and rate limits of the public planner API."""

    permission_classes = [RequiresAdminGroup]

    def get(self, request):
        return Response(PlannerSettingsSerializer(PlannerSettings.load()).data)

    def patch(self, request):
        serializer = PlannerSettingsSerializer(
            PlannerSettings.load(), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        access.forget_cached_access()
        return Response(serializer.data)


class ApiKeyViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Keys for developers. Revoke with is_active=false; the plain key is shown once."""

    permission_classes = [RequiresAdminGroup]
    serializer_class = ApiKeySerializer
    queryset = ApiKey.objects.select_related('created_by')
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw = f'odt_{secrets.token_urlsafe(32)}'
        key = serializer.save(
            prefix=raw[:12], key_hash=access.hash_key(raw), created_by=request.user
        )
        return Response({**ApiKeySerializer(key).data, 'key': raw}, status=status.HTTP_201_CREATED)


class IpBanViewSet(viewsets.ModelViewSet):
    permission_classes = [RequiresAdminGroup]
    serializer_class = IpBanSerializer
    queryset = IpBan.objects.select_related('created_by')
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
        access.forget_cached_access()

    def perform_update(self, serializer):
        serializer.save()
        access.forget_cached_access()

    def perform_destroy(self, instance):
        instance.delete()
        access.forget_cached_access()


class StatsView(generics.GenericAPIView):
    """Base of the request-log views: Admin only, filtered by ``filters_class``."""

    permission_classes = [RequiresAdminGroup]
    filters_class = StatsFilterSerializer

    def filtered_rows(self):
        filters = self.filters_class(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        params = filters.validated_data
        rows = request_logs.filtered(
            params['since'],
            params['until'],
            endpoint=params.get('endpoint'),
            api_key=params.get('api_key'),
            ip=params.get('ip'),
            status=params.get('status'),
        )
        return rows, params


class PlannerRequestLogListView(StatsView, generics.ListAPIView):
    """Requests of the last 30 days (default: the last 24 hours), newest first."""

    filters_class = RequestLogFilterSerializer
    serializer_class = PlannerRequestLogSerializer

    def get_queryset(self):
        return self.filtered_rows()[0].select_related('api_key')


class DailyStatsView(generics.GenericAPIView):
    """Requests per day by status class (no IPs); default: the last 30 days."""

    permission_classes = [RequiresAdminGroup]

    def get(self, request):
        filters = DailyStatsFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        params = filters.validated_data
        date_to = params.get('date_to', timezone.now().date())
        date_from = params.get('date_from', date_to - timedelta(days=29))
        days = request_logs.daily_stats(
            date_from, date_to, endpoint=params.get('endpoint'), api_key=params.get('api_key')
        )
        return self.get_paginated_response(self.paginate_queryset(days))


class StatsSummaryView(StatsView):
    """Totals for the dashboard tiles (raw log; default: the last 24 hours)."""

    def get(self, request):
        rows, params = self.filtered_rows()
        return Response(
            {'since': params['since'], 'until': params['until'], **request_logs.summary(rows)}
        )


class StatsTimeseriesView(StatsView):
    """Requests per minute, hour or day for a chart, split by status class or endpoint."""

    filters_class = TimeseriesFilterSerializer

    def get(self, request):
        rows, params = self.filtered_rows()
        series = request_logs.timeseries(
            rows, params['since'], params['until'], params['interval'], params['split']
        )
        return Response({'since': params['since'], 'until': params['until'], **series})


class StatsEndpointsView(StatsView):
    """Requests, errors and latency per endpoint, busiest first."""

    def get(self, request):
        rows, _ = self.filtered_rows()
        return self.get_paginated_response(self.paginate_queryset(request_logs.endpoints(rows)))


class TopClientsView(StatsView):
    """The busiest IP addresses or API keys, with what they sent and whether they are banned."""

    filters_class = TopClientsFilterSerializer

    def get(self, request):
        rows, params = self.filtered_rows()
        total = rows.count()
        page = self.paginate_queryset(request_logs.top_clients(rows, by=params['by']))
        banned = access.banned_among(row.get('client_ip') for row in page)
        clients = []
        for row in page:
            client = {**row, 'share': request_logs.share(row['requests'], total)}
            if params['by'] == 'api_key':
                client['api_key'] = {
                    'id': client.pop('api_key'),
                    'name': client.pop('api_key__name'),
                    'prefix': client.pop('api_key__prefix'),
                }
            else:
                client['banned'] = row['client_ip'] in banned
            clients.append(client)
        return self.get_paginated_response(clients)


class UserAgentsView(StatsView):
    """User-Agents by requests: telling apps from scrapers behind one address."""

    def get(self, request):
        rows, _ = self.filtered_rows()
        page = self.paginate_queryset(request_logs.user_agents(rows))
        return self.get_paginated_response(
            [
                {'user_agent': row['agent'], 'requests': row['requests'], 'ips': row['ips']}
                for row in page
            ]
        )
