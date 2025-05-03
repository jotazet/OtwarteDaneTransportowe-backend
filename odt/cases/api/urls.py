from rest_framework import routers
from django.urls import path, include
from cases.api.views import PublicTransportViewSet, DataFeedbackViewSet, PublicTransportFeedStatusViewSet

app_name = "cases"

router = routers.DefaultRouter()
router.register(r"status", PublicTransportViewSet, basename="case")
router.register(r"data", DataFeedbackViewSet, basename="data")
router.register(r'feed-status', PublicTransportFeedStatusViewSet, basename='feed-status')
urlpatterns = [
    path("api/", include(router.urls)),
]