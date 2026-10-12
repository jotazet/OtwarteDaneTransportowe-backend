"""The project's URLs plus probe endpoints exercising the public access layer."""

from django.urls import include, path
from rest_framework import serializers
from rest_framework.response import Response

from planner.access import PlannerPublicView


class LookupProbe(PlannerPublicView):
    def get(self, request):
        return Response({'ok': True})


class OtpProbe(PlannerPublicView):
    uses_otp = True
    limit_group = 'realtime'

    def get(self, request):
        return Response({'ok': True})


class _Params(serializers.Serializer):
    n = serializers.IntegerField(min_value=1)


class ValidatingProbe(PlannerPublicView):
    def get(self, request):
        _Params(data=request.query_params).is_valid(raise_exception=True)
        return Response({'ok': True})


urlpatterns = [
    path('', include('OtwarteDaneTransportowe.urls')),
    path('api/planner/v1/test-lookup/', LookupProbe.as_view(), name='test-lookup'),
    path('api/planner/v1/test-otp/', OtpProbe.as_view(), name='test-otp'),
    path('api/planner/v1/test-validate/', ValidatingProbe.as_view(), name='test-validate'),
]
