from datetime import timedelta

from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ValidationError
from django.db.models import OuterRef, Q, Subquery, Prefetch

from OtwarteDaneTransportowe.auth_roles import (
    IsFeedParticipant,
    can_add_feeds,
    can_confirm_feeds,
    can_edit_realtime_submission_content,
    can_edit_static_feed_source,
    is_admin,
    is_helper_reviewer,
    patch_request_includes_submission_content,
)
from data_manager.api.serializers import (
    AdminFeedSubmissionSerializer,
    AdminRealtimeSubmissionSerializer,
    EligibleRealtimeStaticSubmissionSerializer,
    FeedFetchErrorSerializer,
    FeedSubmissionListSerializer,
    FeedSubmissionSerializer,
    FeedSubmissionWriteSerializer,
    FeedDetailSerializer,
    OrganizationFeedsSerializer,
    OrganizationFeedsSummarySerializer,
    ProxyManagedFeedListSerializer,
    RealtimeEndpointRTSerializer,
    RealtimeSubmissionSerializer,
    RealtimeSubmissionWriteSerializer,
    STATIC_ENTRY_SOURCE_FIELDS,
    StaticFeedEntrySerializer,
    UserFeedSubmissionListSerializer,
    UserRealtimeSubmissionSerializer,
)
from data_manager.models import (
    FeedFetchError,
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


class FetchErrorPagination(PageNumberPagination):
    """Pagination for fetch-error listings so a single card never loads everything."""

    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


def _days_since(request, default: int = 7):
    raw = request.query_params.get('days', default)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = default
    days = max(1, min(days, 365))
    return timezone.now() - timedelta(days=days)


def _filter_fetch_errors(request, qs):
    """Apply shared query params (error_type, source, endpoint_type) to a FeedFetchError qs."""
    error_type = request.query_params.get('error_type')
    if error_type:
        qs = qs.filter(error_type=error_type)
    source = request.query_params.get('source')
    if source == 'static':
        qs = qs.filter(static_entry__isnull=False)
    elif source == 'realtime':
        qs = qs.filter(endpoint_rt__isnull=False)
    endpoint_type = request.query_params.get('endpoint_type')
    if endpoint_type:
        qs = qs.filter(endpoint_rt__endpoint_type=endpoint_type)
    return qs


def _serialize_error_queryset(request, qs):
    qs = qs.select_related(
        'static_entry__submission__transport_organization',
        'endpoint_rt__submission__transport_organization',
    )
    paginator = FetchErrorPagination()
    page = paginator.paginate_queryset(qs, request)
    serializer = FeedFetchErrorSerializer(page, many=True, context={'request': request})
    return paginator.get_paginated_response(serializer.data)


class ProxyManagedFeedListView(APIView):
    """
    Single paginated list of all proxy-managed feeds (static + realtime)
    that can be paused or resumed by operators.
    """
    permission_classes = [IsAuthenticated]
    pagination_class = FetchErrorPagination

    def get(self, request):
        static_qs = (
            StaticFeedEntry.objects
            .filter(hide_original=True, url__isnull=False)
            .exclude(url='')
            .select_related('submission', 'submission__transport_organization', 'submission__submitted_by')
        )
        rt_qs = (
            RealtimeEndpointRT.objects
            .filter(hide_original=True)
            .select_related('submission', 'submission__transport_organization', 'submission__submitted_by')
        )
        if not can_confirm_feeds(request.user):
            static_qs = static_qs.filter(submission__submitted_by=request.user)
            rt_qs = rt_qs.filter(submission__submitted_by=request.user)

        items = [
            ProxyManagedFeedListSerializer.from_static_entry(entry)
            for entry in static_qs
        ]
        items.extend(
            ProxyManagedFeedListSerializer.from_realtime_endpoint(endpoint)
            for endpoint in rt_qs
        )
        items.sort(
            key=lambda row: (
                row['region'] or '',
                row['organization'] or '',
                row['feed_name'] or '',
                row['source'],
                row['id'],
            )
        )

        paginator = FetchErrorPagination()
        page = paginator.paginate_queryset(items, request)
        serializer = ProxyManagedFeedListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class FetchErrorListView(generics.ListAPIView):
    """
    Global, paginated list of feed fetch errors across all feeds.

    Operators (Helper/Admin) see every error; a DataProvider sees only errors
    for feeds they submitted. Supports query params: days (default 7, max 365),
    error_type, source (static|realtime), endpoint_type, page, page_size.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = FeedFetchErrorSerializer
    pagination_class = FetchErrorPagination

    def get_queryset(self):
        request = self.request
        qs = (
            FeedFetchError.objects
            .filter(occurred_at__gte=_days_since(request))
            .select_related(
                'static_entry__submission__transport_organization',
                'endpoint_rt__submission__transport_organization',
            )
        )
        if not can_confirm_feeds(request.user):
            qs = qs.filter(
                Q(static_entry__submission__submitted_by=request.user)
                | Q(endpoint_rt__submission__submitted_by=request.user)
            )
        qs = _filter_fetch_errors(request, qs)
        return qs.order_by('-occurred_at')


def _request_changes_static_source(request) -> bool:
    """True when the client sent static_entry source fields (url, file, auth, etc.)."""
    nested = request.data.get('static_entry')
    if isinstance(nested, dict) and STATIC_ENTRY_SOURCE_FIELDS & set(nested.keys()):
        return True
    prefix = 'static_entry.'
    for key in request.data:
        if key.startswith(prefix):
            field = key[len(prefix):].split('.')[0]
            if field in STATIC_ENTRY_SOURCE_FIELDS:
                return True
    return False


class PublicFeedDownloadView(APIView):
    """
    Statyczny feed opublikowany: GET /feed/<feed_submission_id>/
    Pliki RT (proxy): GET /feed/rt/<realtime_submission_id>/ — patrz RealtimePublicFeedDownloadView.
    """
    permission_classes = [AllowAny]
    throttle_scope = 'feed_download'

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
    throttle_scope = 'feed_download'

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


class UserSubmissionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not (can_add_feeds(user) or can_confirm_feeds(user)):
            return Response(
                {'detail': 'You do not have permission to view submissions.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        static_qs = (
            FeedSubmission.objects
            .filter(submitted_by=user)
            .select_related('transport_organization', 'submitted_by', 'static_entry')
            .prefetch_related(
                'history',
                'realtime_submissions__endpoints',
                'realtime_submissions__history',
            )
            .order_by('-created_at')
        )
        realtime_qs = (
            RealtimeSubmission.objects
            .filter(submitted_by=user)
            .select_related('transport_organization', 'submitted_by', 'static_submission')
            .prefetch_related('endpoints', 'history')
            .order_by('-created_at')
        )

        transport_org = request.query_params.get('transport_organization')
        if transport_org:
            static_qs = static_qs.filter(transport_organization_id=transport_org)
            realtime_qs = realtime_qs.filter(transport_organization_id=transport_org)

        return Response(
            {
                'user': user.id,
                'static': UserFeedSubmissionListSerializer(static_qs, many=True, context={'request': request}).data,
                'realtime': UserRealtimeSubmissionSerializer(realtime_qs, many=True, context={'request': request}).data,
            }
        )


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
        context = self.get_serializer_context()
        context['restricted_static_edit'] = False
        serializer = self.get_serializer(data=request.data, context=context)
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

        if is_helper_reviewer(request.user) and patch_request_includes_submission_content(request):
            return Response(
                {
                    'detail': (
                        'Helper may only confirm or reject submissions '
                        '(fields: stage, rejection_cause).'
                    ),
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        was_rejected = instance.is_rejected
        restricted_static_edit = not can_edit_static_feed_source(request.user, instance)
        context = self.get_serializer_context()
        context['restricted_static_edit'] = restricted_static_edit
        serializer = self.get_serializer(instance, data=request.data, partial=partial, context=context)
        serializer.is_valid(raise_exception=True)
        submission = serializer.save()

        if (
            not can_confirm_feeds(request.user)
            and was_rejected
            and _request_changes_static_source(request)
        ):
            FeedSubmissionHistory.objects.create(
                submission=submission,
                event_type=FeedSubmissionHistory.EVENT_UPLOADED,
                stage_before=1,
                stage_after=2,
                actor=request.user,
            )

        if can_confirm_feeds(request.user):
            self._admin_stage_transition(request, submission)

        output = FeedSubmissionSerializer(submission, context={'request': request})
        return Response(output.data)

    def _admin_stage_transition(self, request, submission: FeedSubmission):
        rejection_cause = (request.data.get('rejection_cause') or '').strip()
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

    @action(detail=True, methods=['get'], url_path='fetch-errors')
    def fetch_errors(self, request, pk=None):
        submission = self.get_object()
        try:
            entry = submission.static_entry
        except StaticFeedEntry.DoesNotExist:
            qs = FeedFetchError.objects.none()
        else:
            qs = entry.fetch_errors.filter(occurred_at__gte=_days_since(request))
        qs = _filter_fetch_errors(request, qs)
        return _serialize_error_queryset(request, qs.order_by('-occurred_at'))


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

    @action(
        detail=False,
        methods=['get'],
        url_path=r'eligible-static-submissions/(?P<transport_org>[^/.]+)(?:/(?P<data_type>gtfs|netex|other))?',
    )
    def eligible_static_submissions(self, request, transport_org=None, data_type=None):
        get_object_or_404(TransportOrganization, pk=transport_org)

        data_types = [data_type] if data_type else ['gtfs', 'netex', 'other']
        qs = (
            FeedSubmission.objects
            .filter(
                pk__in=completed_submission_ids(),
                transport_organization_id=transport_org,
                data_type__in=data_types,
            )
            .select_related('transport_organization', 'submitted_by')
            .prefetch_related('realtime_submissions')
            .order_by('-created_at')
        )

        if not can_confirm_feeds(request.user):
            qs = qs.filter(submitted_by=request.user)

        eligible = [
            submission
            for submission in qs
            if RealtimeSubmission.allowed_protocols_for_static_data_type(submission.data_type)
            - {rt.protocol for rt in submission.realtime_submissions.all()}
        ]
        serializer = EligibleRealtimeStaticSubmissionSerializer(
            eligible,
            many=True,
            context={'request': request},
        )
        return Response(serializer.data)

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

        if is_helper_reviewer(request.user) and patch_request_includes_submission_content(request):
            return Response(
                {
                    'detail': (
                        'Helper may only confirm or reject submissions '
                        '(fields: stage, rejection_cause).'
                    ),
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        restricted_realtime = not can_edit_realtime_submission_content(
            request.user, instance
        )
        ctx = self.get_serializer_context()
        ctx['restricted_realtime_edit'] = restricted_realtime
        serializer = self.get_serializer(instance, data=request.data, partial=partial, context=ctx)
        serializer.is_valid(raise_exception=True)
        data = {k: v for k, v in serializer.validated_data.items()}
        endpoints_data = data.pop('endpoints', None)
        with transaction.atomic():
            for attr, val in data.items():
                setattr(instance, attr, val)
            if data:
                instance.save()
            if endpoints_data is not None:
                if restricted_realtime:
                    for ep in endpoints_data:
                        obj = instance.endpoints.get(endpoint_type=ep['endpoint_type'])
                        if obj.interval != ep['interval']:
                            obj.interval = ep['interval']
                            obj.full_clean()
                            obj.save(update_fields=['interval'])
                else:
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
        if endpoints_data is not None and not restricted_realtime:
            validate_realtime_submission_task.delay(instance.id)
        if can_confirm_feeds(request.user):
            self._admin_transition(request, instance)
        out = RealtimeSubmissionSerializer(instance, context={'request': request})
        return Response(out.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)

    def _admin_transition(self, request, rts: RealtimeSubmission):
        rejection_cause = (request.data.get('rejection_cause') or '').strip()
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
        if desired_stage == 4:
            from data_manager.tasks import schedule_realtime_endpoint_fetches

            schedule_realtime_endpoint_fetches(rts.id)

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

    @action(detail=True, methods=['get'], url_path='fetch-errors')
    def fetch_errors(self, request, pk=None):
        rts = self.get_object()
        qs = FeedFetchError.objects.filter(
            endpoint_rt__submission=rts,
            occurred_at__gte=_days_since(request),
        )
        qs = _filter_fetch_errors(request, qs)
        return _serialize_error_queryset(request, qs.order_by('-occurred_at'))


class StaticFeedEntryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = StaticFeedEntrySerializer
    queryset = StaticFeedEntry.objects.select_related(
        'submission', 'submission__submitted_by', 'submission__transport_organization',
    )

    def get_queryset(self):
        qs = super().get_queryset()
        if not can_confirm_feeds(self.request.user):
            qs = qs.filter(submission__submitted_by=self.request.user)
        return qs

    def _require_operator(self, request):
        if not can_confirm_feeds(request.user):
            return Response(
                {'detail': 'Only Admin or Helper can pause or resume feed fetching.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _require_proxy_managed(self, entry):
        if not entry.is_proxy_managed:
            return Response(
                {
                    'detail': (
                        'Pause/resume applies only to proxy-managed feeds '
                        '(hide_original=true). Non-proxied feeds are not cached by the server.'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    @action(detail=True, methods=['post'], url_path='pause-fetch')
    def pause_fetch(self, request, pk=None):
        denied = self._require_operator(request)
        if denied:
            return denied
        entry = self.get_object()
        denied = self._require_proxy_managed(entry)
        if denied:
            return denied
        reason = (request.data.get('reason') or '').strip()
        entry.pause_fetch(reason=reason)
        return Response(self.get_serializer(entry).data)

    @action(detail=True, methods=['post'], url_path='resume-fetch')
    def resume_fetch(self, request, pk=None):
        denied = self._require_operator(request)
        if denied:
            return denied
        entry = self.get_object()
        entry.resume_fetch()
        return Response(self.get_serializer(entry).data)


class RealtimeEndpointRTViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = RealtimeEndpointRTSerializer
    queryset = RealtimeEndpointRT.objects.select_related(
        'submission', 'submission__submitted_by', 'submission__transport_organization',
    )

    def get_queryset(self):
        qs = super().get_queryset()
        if not can_confirm_feeds(self.request.user):
            qs = qs.filter(submission__submitted_by=self.request.user)
        return qs

    def _require_operator(self, request):
        if not can_confirm_feeds(request.user):
            return Response(
                {'detail': 'Only Admin or Helper can pause or resume feed fetching.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _require_proxy_managed(self, endpoint):
        if not endpoint.is_proxy_managed:
            return Response(
                {
                    'detail': (
                        'Pause/resume applies only to proxy-managed endpoints '
                        '(hide_original=true). Non-proxied endpoints are not cached by the server.'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    @action(detail=True, methods=['post'], url_path='pause-fetch')
    def pause_fetch(self, request, pk=None):
        denied = self._require_operator(request)
        if denied:
            return denied
        endpoint = self.get_object()
        denied = self._require_proxy_managed(endpoint)
        if denied:
            return denied
        reason = (request.data.get('reason') or '').strip()
        endpoint.pause_fetch(reason=reason)
        from django.core.cache import cache
        from data_manager.tasks import _rt_alive_key

        cache.delete(_rt_alive_key(endpoint.id))
        return Response(self.get_serializer(endpoint).data)

    @action(detail=True, methods=['post'], url_path='resume-fetch')
    def resume_fetch(self, request, pk=None):
        denied = self._require_operator(request)
        if denied:
            return denied
        endpoint = self.get_object()
        endpoint.resume_fetch()
        from django.core.cache import cache
        from data_manager.tasks import _rt_alive_key, schedule_realtime_endpoint_fetches

        cache.delete(_rt_alive_key(endpoint.id))
        schedule_realtime_endpoint_fetches(endpoint.submission_id)
        return Response(self.get_serializer(endpoint).data)
