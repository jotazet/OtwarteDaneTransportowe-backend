from django.urls import include, path
from rest_framework.routers import DefaultRouter

from data_manager.api.views import (
    MyFeedSubmissionViewSet,
    PublishedFeedViewSet,
    RealtimeFeedDownloadView,
    StaticFeedDownloadView,
)

router = DefaultRouter()
# Private – owner only
router.register(r'my-feed-submissions', MyFeedSubmissionViewSet, basename='my-feed-submissions')
# Public – approved feeds only
router.register(r'feeds', PublishedFeedViewSet, basename='published-feeds')

urlpatterns = [
    path('', include(router.urls)),
    # Secure file downloads – served by Django, never via raw MEDIA_URL
    path('feeds/download/static/<int:pk>/', StaticFeedDownloadView.as_view(), name='download-static'),
    path('feeds/download/realtime/<int:pk>/', RealtimeFeedDownloadView.as_view(), name='download-realtime'),
]
