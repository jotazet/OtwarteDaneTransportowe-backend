import pytest
from django.db.utils import IntegrityError
from cases.models import PublicTransport, DataProvider, CaseStatus, DataFeedback

# DataProvider and DataFeedback Model Tests
@pytest.mark.django_db
def test_data_provider_creation_with_minimal_data(db):
    provider = DataProvider.objects.create(name="Provider 1")

    assert provider.pk is not None
    assert provider.name == "Provider 1"
    assert provider.description is None
    assert provider.website is None
    assert provider.contact_email is None

@pytest.mark.django_db
def test_data_provider_creation_with_all_fields(db):
    provider = DataProvider.objects.create(
        name="Provider 1",
        description="Provider description",
        website="https://provider.example.com",
        contact_email="contact@provider.com"
    )

    assert provider.pk is not None
    assert provider.name == "Provider 1"
    assert provider.description == "Provider description"
    assert provider.website == "https://provider.example.com"
    assert provider.contact_email == "contact@provider.com"

@pytest.mark.django_db
def test_data_provider_str_method(db):
    provider = DataProvider.objects.create(name="Provider 1")
    assert str(provider) == "Provider 1"

@pytest.mark.django_db
def test_data_provider_update(db):
    provider = DataProvider.objects.create(name="Provider 1")
    provider.name = "Updated Provider"
    provider.save()

    updated_provider = DataProvider.objects.get(pk=provider.pk)
    assert updated_provider.name == "Updated Provider"

@pytest.mark.django_db
def test_public_transport_case_status_update(db):
    transport_organization = PublicTransport.objects.create(region="City")
    case_status = CaseStatus.objects.get(case=transport_organization)
    case_status.status = '1'
    case_status.save()

    updated_case_status = CaseStatus.objects.get(pk=case_status.pk)
    assert updated_case_status.status == '1'

@pytest.mark.django_db
def test_public_transport_feedback_creation(db):
    transport_organization = PublicTransport.objects.create(region="City")
    feedback = DataFeedback.objects.create(
        transport_organization=transport_organization,
        data_foramt="GTFS",
        description="Feedback description",
        url_to_data="https://data.example.com"
    )

    assert feedback.pk is not None
    assert feedback.transport_organization == transport_organization
    assert feedback.data_foramt == "GTFS"
    assert feedback.description == "Feedback description"
    assert feedback.url_to_data == "https://data.example.com"

@pytest.mark.django_db
def test_public_transport_feedback_update(db):
    transport_organization = PublicTransport.objects.create(region="City")
    feedback = DataFeedback.objects.create(
        transport_organization=transport_organization,
        data_foramt="GTFS",
        description="Initial description"
    )
    feedback.description = "Updated description"
    feedback.save()

    updated_feedback = DataFeedback.objects.get(pk=feedback.pk)
    assert updated_feedback.description == "Updated description"


# PublicTransport Model Tests
@pytest.mark.django_db
def test_public_transport_creation_with_minimal_data(db):
    transport = PublicTransport.objects.create(region="City 1")

    assert transport.pk is not None
    assert transport.region == "City 1"
    assert transport.transport_organization is None
    assert transport.website is None
    assert transport.contact_email is None
    assert transport.provision is None


@pytest.mark.django_db
def test_public_transport_creation_with_all_fields(db):
    transport = PublicTransport.objects.create(
        region="City 1",
        transport_organization="Transport Org",
        website="https://transport.example.com",
        contact_email="contact@transport.com",
        provision="Provision details"
    )

    assert transport.pk is not None
    assert transport.region == "City 1"
    assert transport.transport_organization == "Transport Org"
    assert transport.website == "https://transport.example.com"
    assert transport.contact_email == "contact@transport.com"
    assert transport.provision == "Provision details"


@pytest.mark.django_db
def test_public_transport_with_data_providers(db):
    provider1 = DataProvider.objects.create(name="Provider 1")
    provider2 = DataProvider.objects.create(name="Provider 2")
    transport = PublicTransport.objects.create(region="City 1")
    transport.data_providers.set([provider1, provider2])

    assert transport.data_providers.count() == 2
    assert provider1 in transport.data_providers.all()
    assert provider2 in transport.data_providers.all()


@pytest.mark.django_db
def test_public_transport_remove_data_provider(db):
    provider = DataProvider.objects.create(name="Provider 1")
    transport = PublicTransport.objects.create(region="City 1")
    transport.data_providers.add(provider)

    assert transport.data_providers.count() == 1

    transport.data_providers.remove(provider)
    assert transport.data_providers.count() == 0


# CaseStatus Model Tests
@pytest.mark.django_db
def test_case_status_creation(db):
    transport = PublicTransport.objects.create(region="City 1")
    case_status = CaseStatus.objects.create(case=transport, status='1')

    assert case_status.pk is not None
    assert case_status.case == transport
    assert case_status.status == '1'
    assert case_status.description is None

@pytest.mark.django_db
def test_case_status_creation_with_default_status(db):
    transport = PublicTransport.objects.create(region="City 1")
    case_status = CaseStatus.objects.create(case=transport)

    assert case_status.pk is not None
    assert case_status.case == transport
    assert case_status.status == '0'
    assert case_status.description is None


@pytest.mark.django_db
def test_case_status_creation_with_custom_status(db):
    transport = PublicTransport.objects.create(region="City 1")
    case_status = CaseStatus.objects.create(case=transport, status='1', description="Data requested")

    assert case_status.pk is not None
    assert case_status.case == transport
    assert case_status.status == '1'
    assert case_status.description == "Data requested"


@pytest.mark.django_db
def test_case_status_str_representation(db):
    transport = PublicTransport.objects.create(region="City 1")
    case_status = CaseStatus.objects.create(case=transport, status='3')

    assert str(case_status) == '3' 
