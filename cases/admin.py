from django.contrib import admin

from cases.models import CaseStatus, DataProvider, TransportOrganization


@admin.register(DataProvider)
class DataProviderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'website', 'contact_email')
    search_fields = ('name', 'contact_email')
    ordering = ('name',)


@admin.register(TransportOrganization)
class TransportOrganizationAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'region',
        'transport_organization',
        'website',
        'contact_email',
        'phone_number',
        'is_public',
        'created_at',
    )
    list_filter = ('region', 'is_public', 'created_at')
    search_fields = ('region', 'transport_organization', 'contact_email', 'phone_number')
    filter_horizontal = ('data_providers',)
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Basic Information', {'fields': ('region', 'transport_organization', 'is_public')}),
        ('Contact Details', {'fields': ('website', 'contact_email', 'phone_number')}),
        ('Data Providers', {'fields': ('data_providers',)}),
        ('Timestamps', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )


@admin.register(CaseStatus)
class CaseStatusAdmin(admin.ModelAdmin):
    list_display = ('id', 'case', 'status', 'date', 'get_description_preview')
    list_filter = ('status', 'date')
    search_fields = ('case__region', 'case__transport_organization', 'description')
    readonly_fields = ('date',)
    date_hierarchy = 'date'

    def get_description_preview(self, obj):
        if obj.description:
            return obj.description[:150] + '...' if len(obj.description) > 150 else obj.description
        return '-'

    get_description_preview.short_description = 'Description Preview'

    fieldsets = (
        ('Case Information', {'fields': ('case', 'status')}),
        ('Details', {'fields': ('description', 'date')}),
    )
