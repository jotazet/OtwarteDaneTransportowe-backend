from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from data_manager.api.serializers import (
    FeedSubmissionListSerializer,
    FeedSubmissionSerializer,
    FeedSubmissionWriteSerializer,
    PublishedFeedSerializer,
)
from data_manager.models import FeedSubmission, RealtimeEndpoint, StaticFeedEntry


# ---------------------------------------------------------------------------
# Custom permissions
# ---------------------------------------------------------------------------

class IsOwner(BasePermission):
    """Allows access only to the user who submitted the feed."""
    def has_object_permission(self, request, view, obj):
        return obj.submitted_by == request.user


class IsOwnerOrAdmin(BasePermission):
    """Allows access to the owner or any admin user."""
    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or obj.submitted_by == request.user


# ---------------------------------------------------------------------------
# My submissions – private, owner-only
# ---------------------------------------------------------------------------

class MyFeedSubmissionViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    Private endpoint – authenticated user sees and manages ONLY their own submissions.

    GET  /my-feed-submissions/                    → list own submissions (all stages)
    POST /my-feed-submissions/                    → create new submission
    GET  /my-feed-submissions/{id}/               → detail of own submission
    POST /my-feed-submissions/{id}/advance-stage/ → admin-only: advance to next stage
    """

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = (
            FeedSubmission.objects.filter(submitted_by=self.request.user)
            .select_related('transport_organization', 'submitted_by')
            .prefetch_related('static_entry', 'realtime_entry__endpoints')
            .order_by('-created_at')
        )
        data_type = self.request.query_params.get('data_type')
        if data_type:
            qs = qs.filter(data_type=data_type)
        feed_kind = self.request.query_params.get('feed_kind')
        if feed_kind:
            qs = qs.filter(feed_kind=feed_kind)
        return qs

    def get_permissions(self):
        if self.action == 'advance_stage':
            return [IsAuthenticated(), IsAdminUser()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.action == 'list':
            return FeedSubmissionListSerializer
        if self.action in ('retrieve', 'advance_stage'):
            return FeedSubmissionSerializer
        return FeedSubmissionWriteSerializer

    def get_object(self):
        obj = super().get_object()
        # Extra safety: non-admin users can only touch their own objects
        if not self.request.user.is_staff and obj.submitted_by != self.request.user:
            raise PermissionDenied('You do not have permission to access this submission.')
        return obj

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        submission = serializer.save()
        output = FeedSubmissionSerializer(submission, context={'request': request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='advance-stage',
            permission_classes=[IsAuthenticated, IsAdminUser])
    def advance_stage(self, request, pk=None):
        """Admin-only: advance a submission to the next stage."""
        submission = self.get_object()
        current = submission.current_stage
        now = timezone.now()

        stage_fields = [
            'stage_upload_at',
            'stage_verification_at',
            'stage_confirmation_at',
            'stage_complete_at',
        ]
        if current >= 4:
            return Response(
                {'detail': 'Submission is already at the final stage.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        FeedSubmission.objects.filter(pk=submission.pk).update(
            **{stage_fields[current]: now}
        )
        submission.refresh_from_db()
        return Response(FeedSubmissionSerializer(submission, context={'request': request}).data)


# ---------------------------------------------------------------------------
# Public endpoint – only fully approved feeds, no sensitive fields
# ---------------------------------------------------------------------------

class PublishedFeedViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    Public read-only endpoint.
    Returns ONLY feeds that have passed all 4 stages (stage_complete_at is set).
    Never exposes auth credentials, original hidden URLs, or internal stage timestamps.

    GET /feeds/          → list published feeds
    GET /feeds/{id}/     → detail of a published feed
    """

    serializer_class = PublishedFeedSerializer
    # No authentication required – these are intentionally public
    permission_classes = []

    def get_queryset(self):
        qs = (
            FeedSubmission.objects
            .filter(stage_complete_at__isnull=False)   # only fully approved
            .select_related('transport_organization', 'submitted_by')
            .prefetch_related('static_entry', 'realtime_entry__endpoints')
            .order_by('-stage_complete_at')
        )
        org = self.request.query_params.get('transport_organization')
        if org:
            qs = qs.filter(transport_organization_id=org)
        data_type = self.request.query_params.get('data_type')
        if data_type:
            qs = qs.filter(data_type=data_type)
        feed_kind = self.request.query_params.get('feed_kind')
        if feed_kind:
            qs = qs.filter(feed_kind=feed_kind)
        return qs


# ---------------------------------------------------------------------------
# Secure file download views – gate-kept, never via raw MEDIA_URL
# ---------------------------------------------------------------------------

class _SecureFileDownloadBase(APIView):
    """
    Only serves files whose parent submission is fully approved
    (stage_complete_at is set).  No auth required to download published feeds,
    but the file is served through Django – the raw MEDIA_ROOT path is never
    exposed publicly, so guessing a path does not grant access to unpublished files.
    """
    permission_classes = []

    def _approved_or_404(self, submission):
        if not submission.stage_complete_at:
            raise Http404


class StaticFeedDownloadView(_SecureFileDownloadBase):
    """GET /api/data_manager/feeds/download/static/<pk>/"""

    def get(self, request, pk):
        try:
            entry = StaticFeedEntry.objects.select_related('submission').get(pk=pk)
        except StaticFeedEntry.DoesNotExist:
            raise Http404

        self._approved_or_404(entry.submission)

        # hide_original=True → serve cached copy; file upload → serve the file
        feed_file = entry.cached_file or entry.file
        if not feed_file:
            raise Http404

        return FileResponse(
            feed_file.open('rb'),
            as_attachment=True,
            filename=feed_file.name.split('/')[-1],
        )


class RealtimeFeedDownloadView(_SecureFileDownloadBase):
    """GET /api/data_manager/feeds/download/realtime/<pk>/"""

    def get(self, request, pk):
        try:
            endpoint = RealtimeEndpoint.objects.select_related(
                'entry__submission'
            ).get(pk=pk)
        except RealtimeEndpoint.DoesNotExist:
            raise Http404

        self._approved_or_404(endpoint.entry.submission)

        if not endpoint.cached_file:
            raise Http404

        return FileResponse(
            endpoint.cached_file.open('rb'),
            as_attachment=True,
            filename=endpoint.cached_file.name.split('/')[-1],
        )
