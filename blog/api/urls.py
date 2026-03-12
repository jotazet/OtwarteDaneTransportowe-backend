from django.urls import include, path
from rest_framework.routers import DefaultRouter

from blog.api.views import PostViewSet, ReactionViewSet

router = DefaultRouter()
router.register(r'posts', PostViewSet)
router.register(r'reactions', ReactionViewSet)

urlpatterns = [
    path('', include(router.urls)),
]

