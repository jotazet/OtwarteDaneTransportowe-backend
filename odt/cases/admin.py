from django.contrib import admin
from cases.models import PublicTransport, DataProvider, CaseStatus, DataFeedback

@admin.register(PublicTransport)
class PublicTransportAdmin(admin.ModelAdmin):
    list_display = ('id', 'region', 'transport_organization', 'website', 'contact_email', 'created_at', 'updated_at')
    search_fields = ('region', 'contact_email', 'transport_organization', 'website')
    ordering = ('-region',)
    date_hierarchy = 'updated_at'

@admin.register(DataProvider)
class DataProviderAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'description', 'website', 'contact_email')
    search_fields = ('name', 'contact_email')
    ordering = ('-name',)

@admin.register(CaseStatus)
class CaseStatusAdmin(admin.ModelAdmin):
    list_display = ('id', 'case__region', 'case__transport_organization', 'status', 'date', 'description')
    search_fields = ('status', 'case__region', 'case__transport_organization')
    ordering = ('-date', 'case__region', 'case__transport_organization')
    date_hierarchy = 'date'

@admin.register(DataFeedback)
class DataFeedbackAdmin(admin.ModelAdmin):
    list_display = ('id', 'transport_organization', 'data_foramt', 'file', 'url_to_data', 'uploaded_at', 'updated_at')
    search_fields = ('transport_organization__region', 'transport_organization__transport_organization', 'data_foramt')
    ordering = ('-uploaded_at',)
    date_hierarchy = 'uploaded_at'