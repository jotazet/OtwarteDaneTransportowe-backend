from django.urls import include, path
from rest_framework.routers import DefaultRouter

from blog.api.views import MyReactionsView, PostViewSet, ReactionViewSet

router = DefaultRouter()
router.register(r'posts', PostViewSet)

urlpatterns = [
    path('', include(router.urls)),
    # Must be declared before the <int:post_id> route (it never matches 'mine',
    # but keeping them adjacent makes the precedence obvious).
    path('reactions/mine/', MyReactionsView.as_view(), name='my-reactions'),
    path('reactions/<int:post_id>/', ReactionViewSet.as_view({'post': 'create'}), name='post-reactions'),
]
