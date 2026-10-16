from datetime import timedelta

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from blog.api.serializers import (
    PostListSerializer,
    PostSerializer,
    ReactionSerializer,
    reaction_count_annotations,
)
from blog.models import Post, Reaction
from OtwarteDaneTransportowe.auth_roles import IsEditorOrOwnBloggerOrReadOnly
from OtwarteDaneTransportowe.request_ip import get_client_ip


class PostDefaultPagination(PageNumberPagination):
    page_size = 4
    page_size_query_param = 'page_size'
    max_page_size = 50


class PostViewSet(viewsets.ModelViewSet):
    queryset = Post.objects.select_related('author').order_by('-date')
    serializer_class = PostSerializer
    permission_classes = [IsEditorOrOwnBloggerOrReadOnly]
    pagination_class = PostDefaultPagination

    def get_queryset(self):
        """Posts with reaction counts aggregated in SQL.

        Prefetching ``reaction_set`` pulled every Reaction row of every post on
        the page into Python just to count them by type — unbounded work and
        memory for a number the database can produce directly. ``your_reaction``
        gets its own prefetch narrowed to the caller's IP, replacing a per-post
        query with one for the whole page.
        """
        qs = super().get_queryset().annotate(**reaction_count_annotations())
        client_ip = get_client_ip(self.request)
        if client_ip:
            qs = qs.prefetch_related(
                Prefetch(
                    'reaction_set',
                    queryset=Reaction.objects.filter(ip_address=client_ip),
                    to_attr='caller_reactions',
                )
            )
        return qs

    def get_serializer_class(self):
        # Use truncated content for list view, full content elsewhere
        if self.action == 'list':
            return PostListSerializer
        return PostSerializer

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)


class MyReactionsView(APIView):
    """GET /api/blog/reactions/mine/?post_ids=1,2,3

    Returns ``{"<post_id>": "<reaction>"}` for the CALLER only, resolved from
    their own client IP — no other visitor's IP or reaction is exposed.

    Why this exists: ``PostSerializer.your_reaction`` is only meaningful when
    the request comes straight from the visitor's browser. Pages rendered
    server-side (Next.js SSR) reach the API from the frontend server, so that
    field would reflect the SERVER's identity and be identical for everyone.
    Clients must therefore read their own reaction state from here, in the
    browser.
    """

    permission_classes = [AllowAny]
    MAX_POST_IDS = 100

    def get(self, request):
        client_ip = get_client_ip(request)
        if not client_ip:
            return Response({})

        raw = (request.query_params.get('post_ids') or '').split(',')
        post_ids = []
        for value in raw:
            value = value.strip()
            if value.isdigit():
                post_ids.append(int(value))
            if len(post_ids) >= self.MAX_POST_IDS:
                break
        if not post_ids:
            return Response({})

        rows = (
            Reaction.objects.filter(post_id__in=post_ids, ip_address=client_ip)
            .exclude(reaction='')
            .values_list('post_id', 'reaction')
        )
        return Response({str(post_id): reaction for post_id, reaction in rows})


class ReactionViewSet(viewsets.ModelViewSet):
    """POST-only upsert of a per-IP reaction.

    No read endpoints exist: the only route maps POST→create
    (blog/api/urls.py) and http_method_names strips everything else, so
    stored IP addresses are never exposed.
    """

    queryset = Reaction.objects.select_related('post').all()
    serializer_class = ReactionSerializer
    # Reactions can be created/updated without authentication; IP is used to limit duplicates.
    permission_classes = [AllowAny]
    # The daily limit counts only new reactions; this also limits changes.
    throttle_scope = 'reaction'
    http_method_names = ['post', 'options']

    def get_client_ip(self, request):
        return get_client_ip(request)

    def create(self, request, *args, **kwargs):
        """Create or update a reaction for a (post, IP) pair.

        Behavior:
        - Daily limit: 10 reactions per IP across all posts (created in last 24h).
        - If no existing reaction for (post, IP): create new (unless reaction is empty -> no-op).
        - If existing and reaction in payload is empty: set reaction='' (soft-remove).
        - If existing and reaction is different/non-empty: update to new value.
        """
        client_ip = self.get_client_ip(request)
        if not client_ip:
            return Response({'detail': 'Unable to determine client IP.'}, status=400)

        post_id = kwargs.get('post_id')
        if not post_id:
            return Response({'detail': 'post_id is required in the URL.'}, status=400)
        post = get_object_or_404(Post, pk=post_id)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_reaction = serializer.validated_data.get('reaction')
        is_empty = not new_reaction

        try:
            existing = Reaction.objects.get(post=post, ip_address=client_ip)
        except Reaction.DoesNotExist:
            existing = None

        if existing is None:
            if is_empty:
                return Response(status=status.HTTP_204_NO_CONTENT)

            # Daily limit counts only *active* reactions (non-empty reaction)
            today_start = timezone.now() - timedelta(days=1)
            reactions_today = (
                Reaction.objects.filter(ip_address=client_ip, date__gte=today_start)
                .exclude(reaction='')
                .count()
            )

            if reactions_today >= 10:
                return Response(
                    {'detail': 'Daily limit reached. You can add maximum 10 reactions per day.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            reaction = Reaction.objects.create(
                post=post,
                ip_address=client_ip,
                reaction=new_reaction,
            )
            output = self.get_serializer(reaction)
            return Response(output.data, status=status.HTTP_201_CREATED)

        # Existing row found for this (post, IP)
        if is_empty:
            # Soft-remove: keep the row but clear the reaction
            if existing.reaction:
                existing.reaction = ''
                existing.save(update_fields=['reaction'])
            return Response(status=status.HTTP_204_NO_CONTENT)

        if existing.reaction != new_reaction:
            existing.reaction = new_reaction
            existing.save(update_fields=['reaction'])
        output = self.get_serializer(existing)
        return Response(output.data)
