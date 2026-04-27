"""
URL configuration for OtwarteDaneTransportowe project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from data_manager.api.views import PublicFeedDownloadView, RealtimePublicFeedDownloadView

urlpatterns = [
    path('admin/', admin.site.urls),

    path('feed/', PublicFeedDownloadView.as_view(), name='feed-download-base'),
    path('feed/<int:pk>/', PublicFeedDownloadView.as_view(), name='feed-download-info'),
    path('feed/<int:pk>/<str:filename>', PublicFeedDownloadView.as_view(), name='feed-download-file'),
    path('feed/rt/<int:pk>/', RealtimePublicFeedDownloadView.as_view(), name='feed-rt-download-info'),
    path('feed/rt/<int:pk>/<str:filename>', RealtimePublicFeedDownloadView.as_view(), name='feed-rt-download-file'),

    path('api/cases/', include('cases.api.urls')),
    path('api/blog/', include('blog.api.urls')),
    path('api/data_manager/', include('data_manager.api.urls')),

    path('api/auth/token/', TokenObtainPairView.as_view(), name='token-obtain-pair'),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('api-auth/', include('rest_framework.urls')),

    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

# NOTE: MEDIA_URL is intentionally NOT served here.
# All feed file downloads go through the secure
# /api/data_manager/feeds/download/static/<pk>/ and
# /api/data_manager/feeds/download/realtime/<pk>/ views
# which check stage_complete_at before serving the file.
