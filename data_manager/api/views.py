from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ValidationError
from django.db.models import OuterRef, Subquery, Prefetch

from OtwarteDaneTransportowe.auth_roles import (
    IsFeedParticipant,
    can_add_feeds,
    can_confirm_feeds,
    is_admin,
)
from data_manager.api.serializers import (
    AdminFeedSubmissionSerializer,
    AdminRealtimeSubmissionSerializer,
    FeedSubmissionListSerializer,
    FeedSubmissionSerializer,
    FeedSubmissionWriteSerializer,
    FeedDetailSerializer,
    OrganizationFeedsSerializer,
    OrganizationFeedsSummarySerializer,
    RealtimeSubmissionSerializer,
    RealtimeSubmissionWriteSerializer,
)
from data_manager.models import (
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
    completed_realtime_submission_ids,
    completed_submission_ids,
)
from cases.models import TransportOrganization


class PublicFeedDownloadView(APIView):
    """
    Statyczny feed opublikowany: GET /feed/<feed_submission_id>/
    Pliki RT (proxy): GET /feed/rt/<realtime_submission_id>/ — patrz RealtimePublicFeedDownloadView.
    """
    permission_classes = [AllowAny]

    def _get_submission_or_404(self, pk):
        submission = get_object_or_404(FeedSubmission, pk=pk)
        if submission.id not in completed_submission_ids():
            raise Http404("Feed not found or not published.")
        return submission

    def get(self, request, pk=None, filename=None):
        if pk is None:
            return Response({"detail": "Specify a feed ID."}, status=status.HTTP_400_BAD_REQUEST)

        submission = self._get_submission_or_404(pk)

        static_file = None
        if hasattr(submission, 'static_entry'):
            entry = submission.static_entry
            static_file = entry.cached_file or entry.file

        if not filename:
            base = request.build_absolute_uri(f'/feed/{submission.id}/')
            result = {}
            if static_file:
                result['static'] = f"{base}{static_file.name.split('/')[-1]}"
            return Response(result)

        if static_file and static_file.name.split('/')[-1] == filename:
            return FileResponse(static_file.open('rb'), as_attachment=True, filename=filename)

        raise Http404("File not found.")


class RealtimePublicFeedDownloadView(APIView):
    """Opublikowany RealtimeSubmission (GBFS / proxy RT): /feed/rt/<pk>/"""
    permission_classes = [AllowAny]

    def get(self, request, pk=None, filename=None):
        if pk is None:
            return Response({"detail": "Specify a realtime feed ID."}, status=status.HTTP_400_BAD_REQUEST)

        if pk not in completed_realtime_submission_ids():
            raise Http404("Realtime feed not found or not published.")

        rts = get_object_or_404(RealtimeSubmission.objects.prefetch_related('endpoints'), pk=pk)

        dynamic_files = {}
        for ep in rts.endpoints.all():
            if ep.cached_file:
                fn = ep.cached_file.name.split('/')[-1]
                dynamic_files[ep.endpoint_type] = {'filename': fn, 'file': ep.cached_file}

        if not filename:
            base = request.build_absolute_uri(f'/feed/rt/{rts.id}/')
            return Response({
                'dynamic': {
                    t: f"{base}{p['filename']}"
                    for t, p in dynamic_files.items()
                }
            })

        for payload in dynamic_files.values():
            if payload['filename'] == filename:
                return FileResponse(payload['file'].open('rb'), as_attachment=True, filename=filename)

        raise Http404("File not found.")


# ---------------------------------------------------------------------------
# OrganizationViewSet – public feeds
# ---------------------------------------------------------------------------

class OrganizationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]

    def get_queryset(self):
        approved_ids = completed_submission_ids()
        latest_history = FeedSubmissionHistory.objects.filter(submission=OuterRef('pk')).order_by('-created_at')
        feeds_qs = (
            FeedSubmission.objects
            .filter(pk__in=approved_ids)
            .annotate(latest_event=Subquery(latest_history.values('event_type')[:1]))
            .exclude(latest_event=FeedSubmissionHistory.EVENT_REJECTED)
            .select_related('transport_organization', 'submitted_by', 'static_entry')
            .order_by('id')
        )
        published_rt = completed_realtime_submission_ids()
        gbfs_qs = (
            RealtimeSubmission.objects
            .filter(pk__in=published_rt, protocol=RealtimeSubmission.PROTOCOL_GBFS)
            .select_related('transport_organization')
            .prefetch_related('endpoints')
            .order_by('id')
        )
        org_qs = (
            TransportOrganization.objects
            .prefetch_related(
                'data_providers', 'case_status',
                Prefetch('feed_submissions', queryset=feeds_qs, to_attr='feeds'),
                Prefetch('realtime_submissions', queryset=gbfs_qs, to_attr='published_gbfs_feeds'),
            )
            .order_by('region', 'transport_organization')
        )
        return org_qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        published_static = set(completed_submission_ids())
        rt_pub = set(completed_realtime_submission_ids())
        rts = (
            RealtimeSubmission.objects.filter(
                pk__in=rt_pub,
                static_submission_id__in=published_static,
                protocol__in=[RealtimeSubmission.PROTOCOL_GTFS_RT, RealtimeSubmission.PROTOCOL_SIRI],
            )
            .prefetch_related('endpoints')
        )
        context['rt_embed'] = {r.static_submission_id: r for r in rts}
        return context

    def get_serializer_class(self):
        if self.action == 'list':
            return OrganizationFeedsSummarySerializer
        return OrganizationFeedsSerializer


# ---------------------------------------------------------------------------
# FeedSubmissionViewSet
# ---------------------------------------------------------------------------

class FeedSubmissionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsFeedParticipant]
    queryset = FeedSubmission.objects.all().select_related(
        'transport_organization', 'submitted_by', 'static_entry'
    ).prefetch_related(
        'history',
        'realtime_submissions__endpoints',
        'realtime_submissions__history',
    )

    def get_queryset(self):
        qs = super().get_queryset()
        if not can_confirm_feeds(self.request.user):
            qs = qs.filter(submitted_by=self.request.user)
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
        if can_confirm_feeds(self.request.user):
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

        if not can_confirm_feeds(request.user):
            if request.data.get('stage') is not None or request.data.get('rejection_cause'):
                return Response(
                    {'detail': 'Only Admin or Helper can confirm or reject submissions.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if instance.current_stage > 1 or instance.is_rejected:
                return Response(
                    {'detail': 'Cannot edit a submission that has been reviewed or rejected.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        serializer = self.get_serializer(instance, data=request.data, partial=partial, context={'request': request})
        serializer.is_valid(raise_exception=True)
        submission = serializer.save()

        if can_confirm_feeds(request.user):
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
        if is_admin(request.user):
            return super().destroy(request, *args, **kwargs)
        if not can_add_feeds(request.user) or instance.submitted_by_id != request.user.id:
            return Response(
                {'detail': 'Only Admin can delete other users submissions.'},
                status=status.HTTP_403_FORBIDDEN,
            )
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
        """Pobierz plik cache endpointu RT (RealtimeSubmission powiązany ze static submission pk)."""
        submission = self.get_object()
        endpoint = get_object_or_404(
            RealtimeEndpointRT.objects.select_related('submission'),
            pk=endpoint_pk,
            submission__static_submission_id=submission.pk,
        )
        if not endpoint.cached_file:
            raise Http404

        return FileResponse(
            endpoint.cached_file.open('rb'),
            as_attachment=True,
            filename=endpoint.cached_file.name.split('/')[-1],
        )


# ---------------------------------------------------------------------------
# RealtimeSubmissionViewSet
# ---------------------------------------------------------------------------

class RealtimeSubmissionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsFeedParticipant]
    queryset = RealtimeSubmission.objects.all().select_related(
        'transport_organization', 'submitted_by', 'static_submission',
    ).prefetch_related('endpoints', 'history')

    def get_queryset(self):
        qs = super().get_queryset()
        if not can_confirm_feeds(self.request.user):
            qs = qs.filter(submitted_by=self.request.user)
        p = self.request.query_params.get('transport_organization')
        if p:
            qs = qs.filter(transport_organization_id=p)
        return qs

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return RealtimeSubmissionWriteSerializer
        if can_confirm_feeds(self.request.user):
            return AdminRealtimeSubmissionSerializer
        return RealtimeSubmissionSerializer

    def create(self, request, *args, **kwargs):
        from data_manager.tasks import validate_realtime_submission_task

        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        endpoints_data = data.pop('endpoints')
        with transaction.atomic():
            rts = RealtimeSubmission.objects.create(
                **data,
                submitted_by=request.user,
            )
            for ep in endpoints_data:
                if ep.get('auth_type'):
                    ep['hide_original'] = True
                o = RealtimeEndpointRT(submission=rts, **ep)
                o.full_clean()
                o.save()
            RealtimeSubmissionHistory.objects.create(
                submission=rts,
                event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
                stage_before=0,
                stage_after=1,
                actor=request.user,
            )
        validate_realtime_submission_task.delay(rts.id)
        out = RealtimeSubmissionSerializer(rts, context={'request': request})
        return Response(out.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        from data_manager.tasks import validate_realtime_submission_task

        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        if not can_confirm_feeds(request.user):
            if request.data.get('stage') is not None or request.data.get('rejection_cause'):
                return Response(
                    {'detail': 'Only Admin or Helper can confirm or reject submissions.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if instance.current_stage != 1 and not instance.is_rejected:
                return Response(
                    {'detail': 'Można edytować tylko na etapie 1 lub po odrzuceniu.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        serializer = self.get_serializer(instance, data=request.data, partial=partial, context={'request': request})
        serializer.is_valid(raise_exception=True)
        data = {k: v for k, v in serializer.validated_data.items()}
        endpoints_data = data.pop('endpoints', None)
        with transaction.atomic():
            for attr, val in data.items():
                setattr(instance, attr, val)
            if data:
                instance.save()
            if endpoints_data is not None:
                instance.endpoints.all().delete()
                for ep in endpoints_data:
                    if ep.get('auth_type'):
                        ep['hide_original'] = True
                    o = RealtimeEndpointRT(submission=instance, **ep)
                    o.full_clean()
                    o.save()
                RealtimeSubmissionHistory.objects.create(
                    submission=instance,
                    event_type=RealtimeSubmissionHistory.EVENT_UPLOADED,
                    stage_before=1,
                    stage_after=1,
                    actor=request.user,
                )
        if endpoints_data is not None:
            validate_realtime_submission_task.delay(instance.id)
        if can_confirm_feeds(request.user):
            self._admin_transition(request, instance)
        out = RealtimeSubmissionSerializer(instance, context={'request': request})
        return Response(out.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)

    def _admin_transition(self, request, rts: RealtimeSubmission):
        rejection_cause = request.data.get('rejection_cause', '').strip()
        desired_stage = request.data.get('stage')
        if rejection_cause:
            cur = rts.current_stage
            RealtimeSubmissionHistory.objects.create(
                submission=rts,
                event_type=RealtimeSubmissionHistory.EVENT_REJECTED,
                stage_before=cur,
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
        cur = rts.current_stage
        if desired_stage == cur:
            return
        ev = (
            RealtimeSubmissionHistory.EVENT_COMPLETED
            if desired_stage == 4
            else RealtimeSubmissionHistory.EVENT_STAGE_ADVANCED
        )
        RealtimeSubmissionHistory.objects.create(
            submission=rts,
            event_type=ev,
            stage_before=cur,
            stage_after=desired_stage,
            actor=request.user,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if is_admin(request.user):
            return super().destroy(request, *args, **kwargs)
        if not can_add_feeds(request.user) or instance.submitted_by_id != request.user.id:
            return Response(
                {'detail': 'Only Admin can delete other users submissions.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if instance.current_stage != 1 or instance.is_rejected:
            return Response(
                {'detail': 'Cannot delete except at stage 1.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)

