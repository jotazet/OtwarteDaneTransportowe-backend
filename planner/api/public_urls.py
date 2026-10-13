from django.urls import path

from planner.api.lookup_views import (
    AgencyListView,
    RouteSearchView,
    StopSearchView,
    StopsInAreaView,
    StopsNearbyView,
    StopView,
)
from planner.api.public_views import PlanView, StatusView

urlpatterns = [
    path('status/', StatusView.as_view(), name='planner-v1-status'),
    path('plan/', PlanView.as_view(), name='planner-v1-plan'),
    path('stops/', StopsInAreaView.as_view(), name='planner-v1-stops'),
    path('stops/search/', StopSearchView.as_view(), name='planner-v1-stop-search'),
    path('stops/nearby/', StopsNearbyView.as_view(), name='planner-v1-stops-nearby'),
    path('stop/', StopView.as_view(), name='planner-v1-stop'),
    path('routes/', RouteSearchView.as_view(), name='planner-v1-routes'),
    path('agencies/', AgencyListView.as_view(), name='planner-v1-agencies'),
]
