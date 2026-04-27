from rest_framework import viewsets

from OtwarteDaneTransportowe.auth_roles import IsCaseManagerOrReadOnly
from cases.models import CaseStatus, DataProvider, TransportOrganization
from cases.api.serializers import (
    CaseStatusSerializer,
    DataProviderSerializer,
    TransportOrganizationSerializer,
    TransportOrganizationDetailSerializer,
)


class DataProviderViewSet(viewsets.ModelViewSet):
    queryset = DataProvider.objects.all().order_by('name')
    serializer_class = DataProviderSerializer
    permission_classes = [IsCaseManagerOrReadOnly]


class CaseStatusViewSet(viewsets.ModelViewSet):
    queryset = CaseStatus.objects.select_related('case').all()
    serializer_class = CaseStatusSerializer
    permission_classes = [IsCaseManagerOrReadOnly]


class TransportOrganizationViewSet(viewsets.ModelViewSet):
    queryset = (
        TransportOrganization.objects.all()
        .prefetch_related('data_providers', 'case_status')
        .order_by('region', 'transport_organization')
    )
    serializer_class = TransportOrganizationSerializer
    permission_classes = [IsCaseManagerOrReadOnly]

    def get_serializer_class(self):
        # Use detail serializer for retrieve (single object), base serializer for list
        if self.action == 'retrieve':
            return TransportOrganizationDetailSerializer
        return TransportOrganizationSerializer

