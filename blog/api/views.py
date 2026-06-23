from datetime import timedelta

from django.utils import timezone
from django.conf import settings
from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from ipaddress import ip_address, ip_network

from OtwarteDaneTransportowe.auth_roles import IsEditorOrOwnBloggerOrReadOnly
from blog.api.serializers import PostSerializer, ReactionSerializer, PostListSerializer
from blog.models import Post, Reaction


class PostDefaultPagination(PageNumberPagination):
    page_size = 4
    page_size_query_param = 'page_size'
    max_page_size = 50


class PostViewSet(viewsets.ModelViewSet):
    queryset = (
        Post.objects.select_related('author')
        .prefetch_related('reaction_set')
        .all()
        .order_by('-date')
    )
    serializer_class = PostSerializer
    permission_classes = [IsEditorOrOwnBloggerOrReadOnly]
    pagination_class = PostDefaultPagination

    def get_serializer_class(self):
        # Use truncated content for list view, full content elsewhere
        if self.action == 'list':
            return PostListSerializer
        return PostSerializer

    def list(self, request, *args, **kwargs):
        # If no `page` param is provided, return all posts without pagination envelope
        if 'page' not in request.query_params:
            qs = self.get_queryset()
            serializer = self.get_serializer(qs, many=True)
            return Response(serializer.data)
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)


class ReactionViewSet(viewsets.ModelViewSet):
    queryset = Reaction.objects.select_related('post').all()
    serializer_class = ReactionSerializer
    # Reactions can be created/updated without authentication; IP is used to limit duplicates.
    permission_classes = [AllowAny]
    # Only allow POST method to prevent IP address leakage
    http_method_names = ['post', 'options']

    def list(self, request, *args, **kwargs):
        """Disable list endpoint to prevent IP address leakage."""
        return Response(
            {'detail': 'Method not allowed. Only POST is supported for reactions.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )

    def retrieve(self, request, *args, **kwargs):
        """Disable retrieve endpoint to prevent IP address leakage."""
        return Response(
            {'detail': 'Method not allowed. Only POST is supported for reactions.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )

    def get_client_ip(self, request):
        remote = request.META.get('REMOTE_ADDR')
        xff = request.META.get('HTTP_X_FORWARDED_FOR')

        trusted = getattr(settings, 'TRUSTED_PROXY_CIDRS', []) or []
        is_trusted_proxy = False
        if remote and trusted:
            try:
                r_ip = ip_address(remote)
                for cidr in trusted:
                    try:
                        if r_ip in ip_network(str(cidr), strict=False):
                            is_trusted_proxy = True
                            break
                    except Exception:
                        continue
            except Exception:
                is_trusted_proxy = False

        if is_trusted_proxy and xff:
            # X-Forwarded-For can contain multiple IPs: client, proxies...
            candidate = xff.split(',')[0].strip()
            return candidate or remote

        return remote

    def create(self, request, *args, **kwargs):
        """Create or update a reaction for a (post, IP) pair.

        Behavior:
        - Daily limit: 10 reactions per IP across all posts (created in last 24h).
        - If no existing reaction for (post, IP): create new (unless reaction is empty -> no-op).
        - If existing and reaction in payload is empty/blank/null: set reaction=NULL (soft-remove).
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
        is_empty = new_reaction in (None, '')

        try:
            existing = Reaction.objects.get(post=post, ip_address=client_ip)
        except Reaction.DoesNotExist:
            existing = None

        if existing is None:
            if is_empty:
                return Response(status=status.HTTP_204_NO_CONTENT)

            # Daily limit counts only *active* reactions (reaction IS NOT NULL)
            today_start = timezone.now() - timedelta(days=1)
            reactions_today = Reaction.objects.filter(
                ip_address=client_ip,
                date__gte=today_start,
                reaction__isnull=False,
            ).count()

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
            # Soft-remove: keep the row but null out the reaction
            if existing.reaction is not None:
                existing.reaction = None
                existing.save(update_fields=['reaction'])
            return Response(status=status.HTTP_204_NO_CONTENT)

        if existing.reaction != new_reaction:
            existing.reaction = new_reaction
            existing.save(update_fields=['reaction'])
        output = self.get_serializer(existing)
        return Response(output.data)
