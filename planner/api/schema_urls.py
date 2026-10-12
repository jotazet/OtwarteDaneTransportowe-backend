"""Only the public planner routes, for its own OpenAPI document."""

from django.urls import include, path

urlpatterns = [
    path('api/planner/v1/', include('planner.api.public_urls')),
]
