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
from planner.api.schedule_views import DeparturesView, RouteView, StopTimetableView, TripView

urlpatterns = [
    path('status/', StatusView.as_view(), name='planner-v1-status'),
    path('plan/', PlanView.as_view(), name='planner-v1-plan'),
    path('stops/', StopsInAreaView.as_view(), name='planner-v1-stops'),
    path('stops/search/', StopSearchView.as_view(), name='planner-v1-stop-search'),
    path('stops/nearby/', StopsNearbyView.as_view(), name='planner-v1-stops-nearby'),
    path('stop/', StopView.as_view(), name='planner-v1-stop'),
    path('stop/departures/', DeparturesView.as_view(), name='planner-v1-departures'),
    path('stop/timetable/', StopTimetableView.as_view(), name='planner-v1-stop-timetable'),
    path('routes/', RouteSearchView.as_view(), name='planner-v1-routes'),
    path('route/', RouteView.as_view(), name='planner-v1-route'),
    path('trip/', TripView.as_view(), name='planner-v1-trip'),
    path('agencies/', AgencyListView.as_view(), name='planner-v1-agencies'),
]
