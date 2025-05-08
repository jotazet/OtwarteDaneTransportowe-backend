from rest_framework.viewsets import ModelViewSet
from cases.models import PublicTransport, DataFeedback
from cases.api.serializers import PublicTransportSerializer, DataFeedbackSerializer, PublicTransportFeedStatusSerializer
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework import status

class PublicTransportViewSet(ModelViewSet):
    queryset = PublicTransport.objects.all()
    serializer_class = PublicTransportSerializer
    http_method_names = ['get']

class DataFeedbackViewSet(ModelViewSet):
    queryset = DataFeedback.objects.all()
    serializer_class = DataFeedbackSerializer
    http_method_names = ['get']

    @action(detail=False, methods=['get'], url_path='region/(?P<region_id>[^/.]+)')
    def by_region(self, request, region_id=None):
        queryset = self.get_queryset().filter(transport_organization__id=region_id)
        if not queryset.exists():
            raise NotFound(detail=f"Not found.")
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

class PublicTransportFeedStatusViewSet(ModelViewSet):
    queryset = PublicTransport.objects.all()
    serializer_class = PublicTransportFeedStatusSerializer
    http_method_names = ['get']