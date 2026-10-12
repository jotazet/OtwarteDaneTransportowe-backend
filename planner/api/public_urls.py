from django.urls import path

from planner.api.public_views import StatusView

urlpatterns = [
    path('status/', StatusView.as_view(), name='planner-v1-status'),
]
