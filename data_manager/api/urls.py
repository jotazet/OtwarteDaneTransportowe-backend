from django.urls import include, path
from rest_framework.routers import DefaultRouter

from data_manager.api.views import FeedSubmissionViewSet, FeedViewSet

router = DefaultRouter()
router.register(r'feed-submissions', FeedSubmissionViewSet, basename='feed-submission')
router.register(r'feeds', FeedViewSet, basename='feed')

urlpatterns = [
    path('', include(router.urls)),
]
