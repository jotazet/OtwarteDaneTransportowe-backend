from django.urls import include, path
from rest_framework.routers import DefaultRouter

from planner.api.views import (
    BuildScheduleView,
    FeedCandidateListView,
    GraphBuildViewSet,
    PlannerFeedViewSet,
    PlannerStatusView,
)

router = DefaultRouter()
router.register(r'feeds', PlannerFeedViewSet, basename='planner-feed')
router.register(r'builds', GraphBuildViewSet, basename='planner-build')

urlpatterns = [
    path('', include(router.urls)),
    path('feed-candidates/', FeedCandidateListView.as_view(), name='planner-feed-candidates'),
    path('schedule/', BuildScheduleView.as_view(), name='planner-schedule'),
    path('status/', PlannerStatusView.as_view(), name='planner-status'),
]
