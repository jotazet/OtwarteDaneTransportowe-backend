from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticatedOrReadOnly

from cases.models import DataProvider, TransportOrganization
from cases.api.serializers import (
    DataProviderSerializer,
    TransportOrganizationSerializer,
    TransportOrganizationDetailSerializer,
)


class DataProviderViewSet(viewsets.ModelViewSet):
    queryset = DataProvider.objects.all().order_by('name')
    serializer_class = DataProviderSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]


class TransportOrganizationViewSet(viewsets.ModelViewSet):
    queryset = (
        TransportOrganization.objects.all()
        .prefetch_related('data_providers', 'case_status')
        .order_by('region', 'transport_organization')
    )
    serializer_class = TransportOrganizationSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_serializer_class(self):
        # Use detail serializer for retrieve (single object), base serializer for list
        if self.action == 'retrieve':
            return TransportOrganizationDetailSerializer
        return TransportOrganizationSerializer

