from django.contrib import admin
from django.utils.html import format_html

from OtwarteDaneTransportowe.auth_roles import is_admin

from data_manager.models import (
    FeedFetchError,
    FeedSubmission,
    FeedSubmissionHistory,
    RealtimeEndpointRT,
    RealtimeSubmission,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)


class StaticFeedEntryInline(admin.StackedInline):
    model = StaticFeedEntry
    extra = 0
    max_num = None
    fields = (
        'url', 'file', 'is_original', 'hide_original',
        'auth_type', 'auth_value',
        'download_time_1', 'download_time_2',
        'license', 'cached_at', 'uploaded_at',
    )
    readonly_fields = ('cached_at', 'uploaded_at')


class RealtimeEndpointRTInline(admin.TabularInline):
    model = RealtimeEndpointRT
    fk_name = 'submission'
    extra = 0
    fields = (
        'endpoint_type', 'url', 'is_original', 'hide_original',
        'auth_type', 'auth_value', 'interval', 'cached_at',
    )
    readonly_fields = ('cached_at',)


class FeedSubmissionHistoryInline(admin.TabularInline):
    model = FeedSubmissionHistory
    extra = 0
    fields = ('event_type', 'stage_before', 'stage_after', 'actor', 'cause', 'created_at')
    readonly_fields = ('event_type', 'stage_before', 'stage_after', 'actor', 'cause', 'created_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class FeedFetchErrorInline(admin.TabularInline):
    model = FeedFetchError
    fk_name = 'static_entry'
    extra = 0
    fields = ('error_type', 'http_status_code', 'message', 'url_attempted', 'occurred_at')
    readonly_fields = fields
    can_delete = False
    verbose_name = 'Fetch Error (Static)'
    verbose_name_plural = 'Fetch Errors (Static)'

    def has_add_permission(self, request, obj=None):
        return False


class RealtimeSubmissionHistoryInline(admin.TabularInline):
    model = RealtimeSubmissionHistory
    extra = 0
    fields = ('event_type', 'stage_before', 'stage_after', 'actor', 'cause', 'created_at')
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(RealtimeSubmission)
class RealtimeSubmissionAdmin(admin.ModelAdmin):
    list_display = ('id', 'protocol', 'transport_organization', 'static_submission', 'created_at')
    list_filter = ('protocol',)
    inlines = [RealtimeEndpointRTInline, RealtimeSubmissionHistoryInline]


@admin.register(FeedSubmissionHistory)
class FeedSubmissionHistoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'submission', 'event_type', 'stage_before', 'stage_after', 'actor', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('submission__name', 'cause')
    readonly_fields = ('submission', 'event_type', 'stage_before', 'stage_after', 'actor', 'cause', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return is_admin(request.user)


@admin.register(FeedFetchError)
class FeedFetchErrorAdmin(admin.ModelAdmin):
    list_display = ('id', 'get_source', 'error_type', 'http_status_code', 'url_attempted', 'occurred_at')
    list_filter = ('error_type', 'occurred_at')
    search_fields = ('url_attempted', 'message')
    readonly_fields = (
        'static_entry', 'endpoint_rt', 'error_type', 'http_status_code',
        'message', 'url_attempted', 'occurred_at',
    )

    @admin.display(description='Source')
    def get_source(self, obj):
        if obj.static_entry_id:
            return format_html('<span style="color: {};">{}</span>', 'blue', 'static')
        return format_html('<span style="color: {};">{}</span>', 'green', 'realtime')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return is_admin(request.user)


@admin.register(FeedSubmission)
class FeedSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'transport_organization', 'data_type',
        'name', 'get_current_stage', 'get_is_rejected', 'created_at',
    )
    list_filter = ('data_type', 'created_at')
    search_fields = (
        'name',
        'transport_organization__region',
        'transport_organization__transport_organization',
    )
    readonly_fields = (
        'created_at', 'updated_at',
        'get_current_stage', 'get_current_stage_label', 'get_is_rejected', 'get_rejection_cause',
    )
    date_hierarchy = 'created_at'
    inlines = [
        StaticFeedEntryInline,
        FeedSubmissionHistoryInline,
    ]
    actions = ['advance_to_next_stage']

    fieldsets = (
        ('Submission Info', {
            'fields': (
                'transport_organization', 'submitted_by',
                'data_type', 'name', 'note',
            )
        }),
        ('Current Status (computed from history)', {
            'fields': (
                'get_current_stage', 'get_current_stage_label',
                'get_is_rejected', 'get_rejection_cause',
            )
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Stage')
    def get_current_stage(self, obj):
        return obj.current_stage

    @admin.display(description='Stage label')
    def get_current_stage_label(self, obj):
        return obj.current_stage_label

    @admin.display(description='Rejected', boolean=True)
    def get_is_rejected(self, obj):
        return obj.is_rejected

    @admin.display(description='Rejection cause')
    def get_rejection_cause(self, obj):
        return obj.rejection_cause or '—'

    @admin.action(description='Advance selected submissions to next stage')
    def advance_to_next_stage(self, request, queryset):
        updated = 0
        for submission in queryset:
            current = submission.current_stage
            if current < 4 and not submission.is_rejected:
                next_stage = current + 1
                event_type = (
                    FeedSubmissionHistory.EVENT_COMPLETED
                    if next_stage == 4
                    else FeedSubmissionHistory.EVENT_STAGE_ADVANCED
                )
                FeedSubmissionHistory.objects.create(
                    submission=submission,
                    event_type=event_type,
                    stage_before=current,
                    stage_after=next_stage,
                    actor=request.user,
                )
                updated += 1
        self.message_user(request, f'{updated} submission(s) advanced to next stage.')
