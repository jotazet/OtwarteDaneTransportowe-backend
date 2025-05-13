from django.urls import path, include
from django.contrib.auth.models import User
from rest_framework import serializers
from cases.models import CaseStatus, PublicTransport, DataProvider, DataFeedback

class MainDataProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataProvider
        fields = ['id', 'name']

class MainCaseStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = CaseStatus
        fields = ['status', 'date']

class PublicTransportSerializer(serializers.ModelSerializer):
    case_status = MainCaseStatusSerializer(many=True, read_only=True)
    data_providers = MainDataProviderSerializer(many=True, read_only=True)

    class Meta:
        model = PublicTransport
        fields = '__all__'

class BasicPublicTransportSerializer(serializers.ModelSerializer):
    class Meta:
        model = PublicTransport
        fields = ['id', 'region', 'transport_organization', 'provision']
        read_only_fields = ['id', 'region', 'transport_organization', 'provision']

class DataFeedbackSerializer(serializers.ModelSerializer):
    transport_organization = BasicPublicTransportSerializer(read_only=True)
    class Meta:
        model = DataFeedback
        fields = ['id', 'data_foramt', 'url_to_data', 'file', 'uploaded_at', 'updated_at', 'transport_organization']
        read_only_fields = ['id', 'data_foramt', 'url_to_data', 'file', 'uploaded_at', 'updated_at', 'transport_organization']

class BasicDataFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataFeedback
        fields = ['id', 'data_foramt', 'updated_at']
        read_only_fields = ['id', 'data_foramt', 'updated_at']

class PublicTransportFeedStatusSerializer(serializers.ModelSerializer):
    data_feeds = BasicDataFeedbackSerializer(many=True, read_only=True, source='feedback')
    class Meta:
        model = PublicTransport
        fields = ['id', 'region', 'transport_organization', 'data_feeds']
        read_only_fields = ['id', 'region', 'transport_organization', 'data_feeds'] 