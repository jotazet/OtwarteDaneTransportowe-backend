from django.urls import path

from planner.api.public_views import PlanView, StatusView

urlpatterns = [
    path('status/', StatusView.as_view(), name='planner-v1-status'),
    path('plan/', PlanView.as_view(), name='planner-v1-plan'),
]
