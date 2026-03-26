from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser, IsAuthenticated, AllowAny, BasePermission
from rest_framework.response import Response
from django.core.exceptions import ValidationError

from data_manager.api.serializers import (
    AdminFeedSubmissionSerializer,
    FeedSubmissionListSerializer,
    FeedSubmissionSerializer,
    FeedSubmissionWriteSerializer,
    FeedListSerializer,
    FeedDetailSerializer,
)
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpoint,
    StaticFeedEntry,
)


class IsAdminOrOwnerReadOnly(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user and request.user.is_staff:
            return True
        return obj.submitted_by_id == getattr(request.user, 'id', None)


# ---------------------------------------------------------------------------
# FeedSubmissionViewSet – central permissions
# ---------------------------------------------------------------------------

class FeedSubmissionViewSet(viewsets.ModelViewSet):
    """
    Central endpoint – owners read their submissions; admins can manage all.

    GET    /feed-submissions/                      → list submissions
    POST   /feed-submissions/                      → create new submission
    GET    /feed-submissions/{id}/                 → detail of submission
    PUT    /feed-submissions/{id}/                 → full edit (admin can move stage)
    PATCH  /feed-submissions/{id}/                 → partial edit (admin can move stage)
    DELETE /feed-submissions/{id}/                 → delete
    """
    permission_classes = [IsAuthenticated, IsAdminOrOwnerReadOnly]

    def get_queryset(self):
        qs = (
            FeedSubmission.objects
            .select_related('transport_organization', 'submitted_by')
            .prefetch_related(
                'history',
                'static_entries',
                'realtime_entry__endpoints',
            )
        )
        if not self.request.user.is_staff:
            qs = qs.filter(submitted_by=self.request.user)
        data_type = self.request.query_params.get('data_type')
        if data_type:
            qs = qs.filter(data_type=data_type)
        feed_kind = self.request.query_params.get('feed_kind')
        if feed_kind:
            qs = qs.filter(feed_kind=feed_kind)
        transport_org = self.request.query_params.get('transport_organization')
        if transport_org:
            qs = qs.filter(transport_organization_id=transport_org)
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return FeedSubmissionListSerializer
        if self.action in ('create', 'update', 'partial_update'):
            return FeedSubmissionWriteSerializer
        if self.request.user and self.request.user.is_staff:
            return AdminFeedSubmissionSerializer
        return FeedSubmissionSerializer

    def perform_create(self, serializer):
        submission = serializer.save(submitted_by=self.request.user)
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=FeedSubmissionHistory.EVENT_UPLOADED,
            stage_before=1,
            stage_after=2,
            actor=self.request.user,
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = serializer.instance
        output = FeedSubmissionSerializer(instance, context={'request': request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        return self._update_with_history(request, partial=False, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        return self._update_with_history(request, partial=True, *args, **kwargs)

    def _update_with_history(self, request, partial: bool, *args, **kwargs):
        instance = self.get_object()

        if not request.user.is_staff:
            if instance.current_stage > 1 or instance.is_rejected:
                return Response(
                    {'detail': 'Cannot edit a submission that has been reviewed or rejected.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        serializer = self.get_serializer(instance, data=request.data, partial=partial, context={'request': request})
        serializer.is_valid(raise_exception=True)
        submission = serializer.save()

        if request.user.is_staff:
            self._admin_stage_transition(request, submission)

        output = FeedSubmissionSerializer(submission, context={'request': request})
        return Response(output.data)

    def _admin_stage_transition(self, request, submission: FeedSubmission):
        rejection_cause = request.data.get('rejection_cause', '').strip()
        desired_stage = request.data.get('stage')

        if rejection_cause:
            current = submission.current_stage
            FeedSubmissionHistory.objects.create(
                submission=submission,
                event_type=FeedSubmissionHistory.EVENT_REJECTED,
                stage_before=current,
                stage_after=1,
                actor=request.user,
                cause=rejection_cause,
            )
            return

        if desired_stage is None:
            return

        try:
            desired_stage = int(desired_stage)
        except (TypeError, ValueError):
            raise ValidationError({'stage': 'Stage must be an integer.'})

        if desired_stage < 1 or desired_stage > 4:
            raise ValidationError({'stage': 'Stage must be between 1 and 4.'})

        current = submission.current_stage
        if desired_stage == current:
            return

        event_type = (
            FeedSubmissionHistory.EVENT_COMPLETED
            if desired_stage == 4
            else FeedSubmissionHistory.EVENT_STAGE_ADVANCED
        )
        FeedSubmissionHistory.objects.create(
            submission=submission,
            event_type=event_type,
            stage_before=current,
            stage_after=desired_stage,
            actor=request.user,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if not request.user.is_staff:
            if instance.current_stage > 1 or instance.is_rejected:
                return Response(
                    {'detail': 'Cannot delete a submission that has been reviewed or rejected.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        return super().destroy(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# FeedViewSet – list/detail with filtering; admin manages
# ---------------------------------------------------------------------------

class FeedViewSet(viewsets.ModelViewSet):
    def get_permissions(self):
        if self.action in ('update', 'partial_update', 'destroy'):
            return [IsAdminUser()]
        if self.action in ('list', 'retrieve', 'download_static', 'download_realtime'):
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = (
            FeedSubmission.objects
            .select_related('transport_organization')
            .prefetch_related('static_entries', 'realtime_entry__endpoints')
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

    def get_serializer_class(self):
        if self.action == 'list':
            return FeedListSerializer
        if self.action == 'retrieve':
            return FeedDetailSerializer
        return FeedSubmissionSerializer

    @action(detail=True, methods=['get'], url_path='download/static/(?P<endpoint_pk>[^/.]+)')
    def download_static(self, request, pk=None, endpoint_pk=None):
        submission = self.get_object()
        entry = get_object_or_404(StaticFeedEntry, pk=endpoint_pk, submission=submission)
        feed_file = entry.cached_file or entry.file
        if not feed_file:
            raise Http404

        return FileResponse(
            feed_file.open('rb'),
            as_attachment=True,
            filename=feed_file.name.split('/')[-1],
        )

    @action(detail=True, methods=['get'], url_path='download/realtime/(?P<endpoint_pk>[^/.]+)')
    def download_realtime(self, request, pk=None, endpoint_pk=None):
        endpoint = get_object_or_404(
            RealtimeEndpoint.objects.select_related('entry__submission'),
            pk=endpoint_pk,
            entry__submission_id=pk,
        )
        if not endpoint.cached_file:
            raise Http404

        return FileResponse(
            endpoint.cached_file.open('rb'),
            as_attachment=True,
            filename=endpoint.cached_file.name.split('/')[-1],
        )
