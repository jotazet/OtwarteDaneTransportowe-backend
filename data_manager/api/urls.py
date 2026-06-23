from django.urls import include, path
from rest_framework.routers import DefaultRouter

from data_manager.api.views import (
    FeedSubmissionViewSet,
    FetchErrorListView,
    OrganizationViewSet,
    ProxyManagedFeedListView,
    RealtimeEndpointRTViewSet,
    RealtimeSubmissionViewSet,
    StaticFeedEntryViewSet,
    UserSubmissionsView,
)

router = DefaultRouter()
router.register(r'feed-submissions', FeedSubmissionViewSet, basename='feed-submissions')
router.register(r'realtime-submissions', RealtimeSubmissionViewSet, basename='realtime-submissions')
router.register(r'static-feed-entries', StaticFeedEntryViewSet, basename='static-feed-entries')
router.register(r'realtime-endpoints', RealtimeEndpointRTViewSet, basename='realtime-endpoints')
router.register(r'feeds', OrganizationViewSet, basename='feeds')

urlpatterns = [
    path('submissions/', UserSubmissionsView.as_view(), name='user-submissions'),
    path('proxy-feeds/', ProxyManagedFeedListView.as_view(), name='proxy-feeds'),
    path('fetch-errors/', FetchErrorListView.as_view(), name='fetch-errors'),
    path('', include(router.urls)),
]
