from django.urls import include, path
from rest_framework.routers import DefaultRouter

from blog.api.views import PostViewSet, ReactionViewSet

router = DefaultRouter()
router.register(r'posts', PostViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('reactions/<int:post_id>/', ReactionViewSet.as_view({'post': 'create'}), name='post-reactions'),
]
