from django.urls import include, path
from rest_framework.routers import DefaultRouter

from cases.api.views import CaseStatusViewSet, DataProviderViewSet, TransportOrganizationViewSet

router = DefaultRouter()
router.register(r'data-providers', DataProviderViewSet)
router.register(r'case-statuses', CaseStatusViewSet)
router.register(r'transport-organizations', TransportOrganizationViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
