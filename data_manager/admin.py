from django.contrib import admin
from django.utils import timezone

from data_manager.models import FeedSubmission, RealtimeEndpoint, RealtimeFeedEntry, StaticFeedEntry


class StaticFeedEntryInline(admin.StackedInline):
    model = StaticFeedEntry
    extra = 0
    max_num = 1
    fields = (
        'url', 'file', 'hide_original',
        'auth_type', 'auth_value',
        'download_time_1', 'download_time_2',
        'uploaded_at',
    )
    readonly_fields = ('uploaded_at',)


class RealtimeEndpointInline(admin.TabularInline):
    model = RealtimeEndpoint
    extra = 0
    fields = ('endpoint_type', 'url', 'hide_original', 'auth_type', 'auth_value')


class RealtimeFeedEntryInline(admin.StackedInline):
    model = RealtimeFeedEntry
    extra = 0
    max_num = 1
    fields = ('protocol', 'uploaded_at')
    readonly_fields = ('uploaded_at',)
    # Note: endpoints are edited via RealtimeEndpoint's own admin
    show_change_link = True


@admin.register(RealtimeFeedEntry)
class RealtimeFeedEntryAdmin(admin.ModelAdmin):
    list_display = ('id', 'protocol', 'submission', 'uploaded_at')
    list_filter = ('protocol',)
    inlines = [RealtimeEndpointInline]


@admin.register(FeedSubmission)
class FeedSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'transport_organization', 'data_type', 'feed_kind',
        'name', 'get_current_stage_label', 'created_at', 'updated_at',
    )
    list_filter = ('data_type', 'feed_kind', 'created_at')
    search_fields = (
        'name',
        'transport_organization__region',
        'transport_organization__transport_organization',
    )
    readonly_fields = (
        'feed_kind', 'created_at', 'updated_at',
        'current_stage', 'get_current_stage_label',
    )
    date_hierarchy = 'created_at'
    inlines = [StaticFeedEntryInline, RealtimeFeedEntryInline]
    actions = ['advance_to_next_stage']

    fieldsets = (
        ('Submission Info', {
            'fields': (
                'transport_organization', 'submitted_by',
                'data_type', 'feed_kind', 'name', 'note',
            )
        }),
        ('Progress Stages', {
            'fields': (
                'current_stage', 'get_current_stage_label',
                'stage_upload_at', 'stage_verification_at',
                'stage_confirmation_at', 'stage_complete_at',
            )
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Stage')
    def get_current_stage_label(self, obj):
        return obj.current_stage_label

    @admin.action(description='Advance selected submissions to next stage')
    def advance_to_next_stage(self, request, queryset):
        now = timezone.now()
        updated = 0
        stage_fields = [
            'stage_upload_at', 'stage_verification_at',
            'stage_confirmation_at', 'stage_complete_at',
        ]
        for submission in queryset:
            current = submission.current_stage
            if current < 4:
                FeedSubmission.objects.filter(pk=submission.pk).update(
                    **{stage_fields[current]: now}
                )
                updated += 1
        self.message_user(request, f'{updated} submission(s) advanced to next stage.')
