from django.urls import include, path
from rest_framework.routers import DefaultRouter

from data_manager.api.views import FeedSubmissionViewSet, OrganizationViewSet, RealtimeSubmissionViewSet

router = DefaultRouter()
router.register(r'feed-submissions', FeedSubmissionViewSet, basename='feed-submissions')
router.register(r'realtime-submissions', RealtimeSubmissionViewSet, basename='realtime-submissions')
router.register(r'feeds', OrganizationViewSet, basename='feeds')

urlpatterns = [
    path('', include(router.urls)),
]
