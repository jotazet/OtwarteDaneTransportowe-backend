from django.db.models import Prefetch
from rest_framework import viewsets

from cases.api.serializers import (
    CaseStatusSerializer,
    DataProviderSerializer,
    TransportOrganizationDetailSerializer,
    TransportOrganizationSerializer,
)
from cases.models import CaseStatus, DataProvider, TransportOrganization
from OtwarteDaneTransportowe.auth_roles import (
    IsCaseManagerOrReadOnly,
    IsTransportOrganizationManagerOrReadOnly,
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
        # The serializers read these from the prefetch cache: providers by
        # name, statuses newest first (CaseStatus.Meta.ordering).
        .prefetch_related(
            Prefetch('data_providers', queryset=DataProvider.objects.order_by('name')),
            'case_status',
        )
        .order_by('region', 'transport_organization')
    )
    serializer_class = TransportOrganizationSerializer
    permission_classes = [IsTransportOrganizationManagerOrReadOnly]

    def get_serializer_class(self):
        # Use detail serializer for retrieve (single object), base serializer for list
        if self.action == 'retrieve':
            return TransportOrganizationDetailSerializer
        return TransportOrganizationSerializer
