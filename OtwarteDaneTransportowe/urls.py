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
from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import AllowAny, IsAdminUser
from data_manager.api.views import PublicFeedDownloadView, RealtimePublicFeedDownloadView
from OtwarteDaneTransportowe.jwt_views import RolesInTokenObtainPairView, ThrottledTokenRefreshView

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
    path('api/users/', include('accounts.api.urls')),

    path('api/auth/token/', RolesInTokenObtainPairView.as_view(), name='token-obtain-pair'),
    path('api/auth/token/refresh/', ThrottledTokenRefreshView.as_view(), name='token-refresh'),
]

# The API schema and interactive docs are public in development but restricted to
# admins in production to avoid disclosing the full API surface.
_schema_permissions = [AllowAny] if settings.DEBUG else [IsAdminUser]
urlpatterns += [
    path('api/schema/', SpectacularAPIView.as_view(permission_classes=_schema_permissions), name='schema'),
    path(
        'api/schema/swagger-ui/',
        SpectacularSwaggerView.as_view(url_name='schema', permission_classes=_schema_permissions),
        name='swagger-ui',
    ),
    path(
        'api/schema/redoc/',
        SpectacularRedocView.as_view(url_name='schema', permission_classes=_schema_permissions),
        name='redoc',
    ),
]

# The DRF browsable-API login is only needed for interactive development.
if settings.DEBUG:
    urlpatterns += [path('api-auth/', include('rest_framework.urls'))]

# MEDIA_URL is not mounted in full: feed packages must use the authenticated download views.
# Blog post images (public) live under MEDIA_ROOT/blog/ and are safe to serve at MEDIA_URL/blog/.
_blog_media_prefix = '/'.join(s for s in (settings.MEDIA_URL.strip('/'), 'blog') if s)
urlpatterns += [
    re_path(
        rf'^{_blog_media_prefix}/(?P<path>.*)$',
        serve,
        {'document_root': str(Path(settings.MEDIA_ROOT) / 'blog')},
    ),
]
