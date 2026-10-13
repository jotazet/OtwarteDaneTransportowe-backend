from django.urls import include, path
from rest_framework.routers import DefaultRouter

from planner.api.views import (
    ApiKeyViewSet,
    BuildScheduleView,
    DailyStatsView,
    FeedCandidateListView,
    GraphBuildViewSet,
    IpBanViewSet,
    OsmDownloadView,
    PlannerFeedViewSet,
    PlannerRentalSourceViewSet,
    PlannerRequestLogListView,
    PlannerSettingsView,
    PlannerStatusView,
    RentalCandidateListView,
    TopClientsView,
)

router = DefaultRouter()
router.register(r'feeds', PlannerFeedViewSet, basename='planner-feed')
router.register(r'rental-sources', PlannerRentalSourceViewSet, basename='planner-rental-source')
router.register(r'builds', GraphBuildViewSet, basename='planner-build')
router.register(r'api-keys', ApiKeyViewSet, basename='planner-api-key')
router.register(r'ip-bans', IpBanViewSet, basename='planner-ip-ban')

urlpatterns = [
    path('', include(router.urls)),
    path('feed-candidates/', FeedCandidateListView.as_view(), name='planner-feed-candidates'),
    path('rental-candidates/', RentalCandidateListView.as_view(), name='planner-rental-candidates'),
    path('osm/', OsmDownloadView.as_view(), name='planner-osm'),
    path('schedule/', BuildScheduleView.as_view(), name='planner-schedule'),
    path('status/', PlannerStatusView.as_view(), name='planner-status'),
    path('settings/', PlannerSettingsView.as_view(), name='planner-settings'),
    path('requests/', PlannerRequestLogListView.as_view(), name='planner-requests'),
    path('stats/daily/', DailyStatsView.as_view(), name='planner-stats-daily'),
    path('stats/top-clients/', TopClientsView.as_view(), name='planner-stats-top-clients'),
]
