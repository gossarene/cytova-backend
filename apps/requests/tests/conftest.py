"""
Shared fixtures for requests module tests.
"""
import pytest
from decimal import Decimal

from apps.catalog.models import ExamCategory, ExamDefinition, PricingRule, PricingType, SampleType
from apps.partners.models import OrganizationType
from apps.partners.services import PartnerOrganizationService
from apps.patients.models import Patient


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-TEST-001',
        first_name='John',
        last_name='Doe',
        date_of_birth='1990-01-15',
        gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def partner_org(lab_admin, make_request):
    return PartnerOrganizationService.create(
        validated_data={
            'code': 'CLN-TEST',
            'name': 'Test Clinic',
            'organization_type': OrganizationType.CLINIC,
            'contact_person': 'Dr. Test',
            'email': 'dr@testclinic.com',
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
    )


@pytest.fixture()
def inactive_partner(lab_admin, make_request):
    partner = PartnerOrganizationService.create(
        validated_data={
            'code': 'INACT-001',
            'name': 'Inactive Partner',
            'organization_type': OrganizationType.HOSPITAL,
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
    )
    PartnerOrganizationService.deactivate(
        partner=partner,
        deactivated_by=lab_admin,
        request=make_request(lab_admin),
    )
    return partner


@pytest.fixture()
def exam_definition(default_technique):
    cat = ExamCategory.objects.create(name='Hematology', display_order=1)
    return ExamDefinition.objects.create(
        category=cat,
        technique=default_technique,
        code='CBC',
        name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def pricing_rule(exam_definition, lab_admin):
    """Broad exam-only pricing rule for backward compatibility."""
    return PricingRule.objects.create(
        exam_definition=exam_definition,
        pricing_type=PricingType.FIXED_PRICE,
        value=Decimal('75.0000'),
        created_by=lab_admin,
    )
