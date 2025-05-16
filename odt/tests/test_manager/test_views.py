import pytest
from rest_framework.test import APIClient
from rest_framework import status
from cases.models import PublicTransport

@pytest.mark.django_db
class TestPublicTransportViews:
    @pytest.fixture
    def api_client(self):
        return APIClient()

    @pytest.fixture
    def public_transport(self):
        return PublicTransport.objects.create(region="Test Region", transport_organization="Test Org")

    def test_get_public_transport_list(self, api_client):
        PublicTransport.objects.create(region="Region 1", transport_organization="Org 1")
        PublicTransport.objects.create(region="Region 2", transport_organization="Org 2")

        response = api_client.get("/publictransport/feed-status/")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 2

    def test_get_public_transport_detail(self, api_client, public_transport):
        response = api_client.get(f"/publictransport/status/{public_transport.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["region"] == public_transport.region
        assert response.data["transport_organization"] == public_transport.transport_organization

    def test_post_public_transport_not_allowed(self, api_client):
        data = {
            "region": "New Region",
            "transport_organization": "New Org",
        }
        response = api_client.post("/publictransport/status/", data)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_put_public_transport_not_allowed(self, api_client, public_transport):
        data = {
            "region": "Updated Region",
            "transport_organization": "Updated Org",
        }
        response = api_client.put(f"/publictransport/status/{public_transport.id}/", data)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_public_transport_not_allowed(self, api_client, public_transport):
        response = api_client.delete(f"/publictransport/status/{public_transport.id}/")

        assert response.status_code == status.HTTP_403_FORBIDDEN