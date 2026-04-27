from rest_framework import serializers
from cases.models import CaseStatus, DataProvider, TransportOrganization

class DataProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataProvider
        fields = ['id', 'name', 'website', 'contact_email']


class CaseStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = CaseStatus
        fields = ['id', 'case', 'status', 'date', 'description']
        read_only_fields = ['id', 'date']


class TransportOrganizationSerializer(serializers.ModelSerializer):
    # Read: show id + name
    data_providers = serializers.SerializerMethodField()
    # Write: accept list of provider IDs
    data_provider_ids = serializers.PrimaryKeyRelatedField(
        source='data_providers',
        queryset=DataProvider.objects.all(),
        many=True,
        required=False,
        write_only=True,
    )

    latest_status = serializers.SerializerMethodField()

    class Meta:
        model = TransportOrganization
        fields = [
            'id',
            'region',
            'transport_organization',
            'website',
            'contact_email',
            'phone_number',
            'is_public',
            'data_providers',
            'data_provider_ids',
            'created_at',
            'updated_at',
            'latest_status',
        ]
        read_only_fields = ['created_at', 'updated_at', 'latest_status', 'data_providers']

    def get_data_providers(self, obj: TransportOrganization):
        return [{'id': dp.id, 'name': dp.name} for dp in obj.data_providers.all().order_by('name')]

    def get_latest_status(self, obj: TransportOrganization):
        status = obj.case_status.order_by('-date').first()
        if not status:
            return None
        return {
            'id': status.id,
            'status': status.status,
            'status_display': status.get_status_display(),
            'date': status.date,
            'description': status.description,
        }


class TransportOrganizationDetailSerializer(TransportOrganizationSerializer):
    # For detail view: return all statuses instead of just latest_status
    statuses = serializers.SerializerMethodField()

    class Meta(TransportOrganizationSerializer.Meta):
        fields = [
            'id',
            'region',
            'transport_organization',
            'website',
            'contact_email',
            'phone_number',
            'is_public',
            'data_providers',
            'data_provider_ids',
            'created_at',
            'updated_at',
            'statuses',
        ]
        read_only_fields = ['created_at', 'updated_at', 'data_providers', 'statuses']

    def get_statuses(self, obj: TransportOrganization):
        statuses = obj.case_status.order_by('-date')
        return [
            {
                'id': s.id,
                'status': s.status,
                'status_display': s.get_status_display(),
                'date': s.date,
                'description': s.description,
            }
            for s in statuses
        ]

