from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ValidationError
from django.db.models import OuterRef, Subquery, Prefetch

from data_manager.api.serializers import (
    AdminFeedSubmissionSerializer,
    FeedSubmissionListSerializer,
    FeedSubmissionSerializer,
    FeedSubmissionWriteSerializer,
    FeedListSerializer,
    FeedDetailSerializer,
    OrganizationFeedsSerializer,
    OrganizationFeedsSummarySerializer,
)
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpoint,
    StaticFeedEntry,
    completed_submission_ids,
)
from cases.models import TransportOrganization
from cases.api.serializers import TransportOrganizationSerializer


class PublicFeedDownloadView(APIView):
    """
    Handles downloading files securely or listing available files for a feed.
    - GET /feed/<id>/ -> lists available files (static/dynamic) with original urls if applicable.
    - GET /feed/<id>/<filename> -> downloads a specific file.
    Only feeds in 'completed' stage are accessible.
    """
    permission_classes = [AllowAny]

    def _get_submission_or_404(self, pk):
        submission = get_object_or_404(FeedSubmission, pk=pk)
        # Verify it's approved / published
        if submission.id not in completed_submission_ids():
            raise Http404("Feed not found or not published.")
        return submission

    def get(self, request, pk=None, filename=None):
        if pk is None:
            return Response({"detail": "Specify a feed ID."}, status=status.HTTP_400_BAD_REQUEST)

        submission = self._get_submission_or_404(pk)

        static_file = None
        dynamic_files = {}

        if hasattr(submission, 'static_entry'):
            entry = submission.static_entry
            static_file = entry.cached_file or entry.file

        if hasattr(submission, 'realtime_entry'):
            for ep in submission.realtime_entry.endpoints.all():
                if ep.cached_file:
                    ep_filename = ep.cached_file.name.split('/')[-1]
                    dynamic_files[ep.endpoint_type] = {
                        'filename': ep_filename,
                        'file': ep.cached_file,
                    }

        # Handle Directory / Listing request
        if not filename:
            base = request.build_absolute_uri(f'/feed/{submission.id}/')
            result = {}
            if static_file:
                result['static'] = f"{base}{static_file.name.split('/')[-1]}"
            if dynamic_files:
                result['dynamic'] = {
                    endpoint_type: f"{base}{payload['filename']}"
                    for endpoint_type, payload in dynamic_files.items()
                }
            return Response(result)

        # Handle File Download request
        if static_file and static_file.name.split('/')[-1] == filename:
            return FileResponse(static_file.open('rb'), as_attachment=True, filename=filename)

        for payload in dynamic_files.values():
            if payload['filename'] == filename:
                return FileResponse(payload['file'].open('rb'), as_attachment=True, filename=filename)

        raise Http404("File not found.")


class IsAdminOrOwnerReadOnly(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user and request.user.is_staff:
            return True
        return obj.submitted_by_id == getattr(request.user, 'id', None)


# ---------------------------------------------------------------------------
# OrganizationViewSet – for public, read-only feed listings
# ---------------------------------------------------------------------------

class OrganizationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public, read-only endpoint for browsing feeds grouped by Transport Organization.
    - List view (`/api/data_manager/feeds/`) provides a summary of available feed types.
    - Detail view (`/api/data_manager/feeds/{org_id}/`) provides full links to approved feeds.
    """
    permission_classes = [AllowAny]

    def get_queryset(self):
        approved_ids = completed_submission_ids()
        latest_history = FeedSubmissionHistory.objects.filter(submission=OuterRef('pk')).order_by('-created_at')
        feeds_qs = (
            FeedSubmission.objects
            .filter(pk__in=approved_ids)
            .annotate(latest_event=Subquery(latest_history.values('event_type')[:1]))
            .exclude(latest_event=FeedSubmissionHistory.EVENT_REJECTED)
            .select_related('transport_organization', 'submitted_by', 'realtime_entry')
            .select_related('static_entry')
            .prefetch_related('realtime_entry__endpoints')
            .order_by('id')
        )
        org_qs = (
            TransportOrganization.objects
            .prefetch_related(
                'data_providers', 'case_status',
                Prefetch('feed_submissions', queryset=feeds_qs, to_attr='feeds'),
            )
            .order_by('region', 'transport_organization')
        )
        return org_qs

    def get_serializer_class(self):
        if self.action == 'list':
            return OrganizationFeedsSummarySerializer
        if self.action == 'retrieve':
            return OrganizationFeedsSerializer
        return OrganizationFeedsSerializer # Default


# ---------------------------------------------------------------------------
# FeedSubmissionViewSet – for owners and admins to manage submissions
# ---------------------------------------------------------------------------

class FeedSubmissionViewSet(viewsets.ModelViewSet):
    """
    Central endpoint for owners to manage their submissions and for admins to manage all.
    - GET    /api/data_manager/feed-submissions/
    - POST   /api/data_manager/feed-submissions/
    - GET    /api/data_manager/feed-submissions/{id}/
    - PATCH  /api/data_manager/feed-submissions/{id}/
    - DELETE /api/data_manager/feed-submissions/{id}/
    """
    permission_classes = [IsAuthenticated, IsAdminOrOwnerReadOnly]
    queryset = FeedSubmission.objects.all().select_related(
        'transport_organization', 'submitted_by', 'static_entry'
    ).prefetch_related(
        'history', 'realtime_entry__endpoints'
    )

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.request.user.is_staff:
            qs = qs.filter(submitted_by=self.request.user)
        # Filtering based on query params
        data_type = self.request.query_params.get('data_type')
        if data_type:
            qs = qs.filter(data_type=data_type)
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
