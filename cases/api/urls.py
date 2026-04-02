from django.urls import include, path
from rest_framework.routers import DefaultRouter

from cases.api.views import DataProviderViewSet, TransportOrganizationViewSet
from data_manager.api.views import OrganizationViewSet

router = DefaultRouter()
router.register(r'data-providers', DataProviderViewSet)
router.register(r'transport-organizations', TransportOrganizationViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
