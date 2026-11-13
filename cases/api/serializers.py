from rest_framework import serializers
from rest_framework.validators import UniqueTogetherValidator, UniqueValidator

from cases.models import CaseStatus, DataProvider, TransportOrganization

# Error codes (ADR 0006), listed in docs/api/cases.md.
DATA_PROVIDER_NAME_TAKEN = 'cases.data_provider_name_taken'
ORGANIZATION_EXISTS = 'cases.organization_exists'


class DataProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataProvider
        fields = ['id', 'name', 'website', 'contact_email']
        extra_kwargs = {
            'name': {
                'validators': [
                    UniqueValidator(DataProvider.objects.all(), message=DATA_PROVIDER_NAME_TAKEN)
                ]
            }
        }


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
        validators = [
            UniqueTogetherValidator(
                TransportOrganization.objects.all(),
                fields=['region', 'transport_organization'],
                message=ORGANIZATION_EXISTS,
            )
        ]

    def get_data_providers(self, obj: TransportOrganization):
        providers = obj.data_providers.all()
        # The viewset prefetches them ordered by name; an object just created
        # or updated has no prefetch, so order in the query instead.
        if 'data_providers' not in getattr(obj, '_prefetched_objects_cache', {}):
            providers = providers.order_by('name')
        return [{'id': dp.id, 'name': dp.name} for dp in providers]

    def get_latest_status(self, obj: TransportOrganization):
        # Newest first by CaseStatus.Meta.ordering; .all() reads the prefetch.
        status = next(iter(obj.case_status.all()), None)
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
        statuses = obj.case_status.all()  # newest first (Meta.ordering)
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
