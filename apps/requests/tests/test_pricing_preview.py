"""
Tests for the pricing preview endpoint and its consistency with the
final request creation path.

The core guarantee this file locks in is: **the preview resolver and
the final create path produce identical prices for the same input**.
Both call ``RequestPricingResolver``, so the guarantee is structural —
but we still exercise it end-to-end via the HTTP layer so a future
refactor that introduces two distinct code paths will break a test.
"""
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.catalog.models import ExamCategory, ExamDefinition, SampleType
from apps.partners.models import (
    OrganizationType, PartnerExamPrice, PartnerOrganization,
)
from apps.patients.models import Patient
from apps.requests.models import PriceSource, SourceType, BillingMode
from apps.requests.services import AnalysisRequestService


API = '/api/v1/requests'


@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    from apps.tenants.models import (
        Subscription, SubscriptionPlan, SubscriptionStatus, Tenant,
    )

    with django_db_blocker.unblock():
        with schema_context(get_public_schema_name()):
            plan, _ = SubscriptionPlan.objects.get_or_create(
                code='TEST_TRIAL',
                defaults={
                    'name': 'Test Trial',
                    'is_trial': True,
                    'trial_duration_days': 30,
                    'is_public': False,
                },
            )
            tenant = Tenant.objects.get(schema_name=_test_tenant_schema)
            Subscription.objects.get_or_create(
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Biochemistry', display_order=1)


@pytest.fixture()
def exam(category, default_technique):
    return ExamDefinition.objects.create(
        category=category,
        technique=default_technique,
        code='GLU',
        name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def exam_b(category, default_technique):
    return ExamDefinition.objects.create(
        category=category,
        technique=default_technique,
        code='HBA1C',
        name='Glycated Hemoglobin',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('80.0000'),
    )


@pytest.fixture()
def partner():
    return PartnerOrganization.objects.create(
        code='PEP-CLN',
        name='Preview Clinic',
        organization_type=OrganizationType.CLINIC,
    )


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-PREVIEW-001',
        first_name='Preview',
        last_name='Patient',
        date_of_birth='1990-01-01',
        gender='MALE',
        created_by=lab_admin,
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Preview endpoint — direct patient
# ---------------------------------------------------------------------------

class TestPreviewDirectPatient:

    def test_returns_unit_price_for_each_exam(
        self, admin_client, exam, exam_b,
    ):
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.DIRECT_PATIENT,
                'exam_definition_ids': [str(exam.id), str(exam_b.id)],
            },
            format='json',
        )
        assert resp.status_code == 200, resp.content
        items = _data(resp)['items']
        assert len(items) == 2

        by_code = {i['exam_code']: i for i in items}
        assert by_code['GLU']['unit_price'] == '50.0000'
        assert by_code['GLU']['billed_price'] == '50.0000'
        assert by_code['GLU']['price_source'] == PriceSource.DEFAULT_PRICE
        assert by_code['HBA1C']['billed_price'] == '80.0000'

    def test_partner_id_must_be_null_for_direct_patient(
        self, admin_client, exam, partner,
    ):
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.DIRECT_PATIENT,
                'partner_organization_id': str(partner.id),
                'exam_definition_ids': [str(exam.id)],
            },
            format='json',
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Preview endpoint — partner organization
# ---------------------------------------------------------------------------

class TestPreviewPartnerOrganization:

    def test_uses_agreed_price_when_present(
        self, admin_client, exam, partner,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': str(partner.id),
                'exam_definition_ids': [str(exam.id)],
            },
            format='json',
        )
        assert resp.status_code == 200
        item = _data(resp)['items'][0]
        assert item['unit_price'] == '50.0000'
        assert item['billed_price'] == '35.0000'
        assert item['price_source'] == PriceSource.PARTNER_AGREED_PRICE

    def test_falls_back_to_unit_price_without_agreed(
        self, admin_client, exam, partner,
    ):
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': str(partner.id),
                'exam_definition_ids': [str(exam.id)],
            },
            format='json',
        )
        assert resp.status_code == 200
        item = _data(resp)['items'][0]
        assert item['billed_price'] == '50.0000'
        assert item['price_source'] == PriceSource.DEFAULT_PRICE

    def test_ignores_inactive_agreed(self, admin_client, exam, partner):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam,
            agreed_price=Decimal('35.0000'), is_active=False,
        )
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': str(partner.id),
                'exam_definition_ids': [str(exam.id)],
            },
            format='json',
        )
        item = _data(resp)['items'][0]
        assert item['billed_price'] == '50.0000'
        assert item['price_source'] == PriceSource.DEFAULT_PRICE

    def test_mixed_resolution_for_multiple_exams(
        self, admin_client, exam, exam_b, partner,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': str(partner.id),
                'exam_definition_ids': [str(exam.id), str(exam_b.id)],
            },
            format='json',
        )
        items = {i['exam_code']: i for i in _data(resp)['items']}
        assert items['GLU']['billed_price'] == '35.0000'
        assert items['GLU']['price_source'] == PriceSource.PARTNER_AGREED_PRICE
        assert items['HBA1C']['billed_price'] == '80.0000'
        assert items['HBA1C']['price_source'] == PriceSource.DEFAULT_PRICE

    def test_partner_id_required_for_partner_org(
        self, admin_client, exam,
    ):
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'exam_definition_ids': [str(exam.id)],
            },
            format='json',
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Preview input validation
# ---------------------------------------------------------------------------

class TestPreviewValidation:

    def test_empty_exam_list_rejected(self, admin_client):
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {'source_type': SourceType.DIRECT_PATIENT, 'exam_definition_ids': []},
            format='json',
        )
        assert resp.status_code == 400

    def test_duplicate_exam_ids_rejected(self, admin_client, exam):
        resp = admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.DIRECT_PATIENT,
                'exam_definition_ids': [str(exam.id), str(exam.id)],
            },
            format='json',
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Preview ↔ final create consistency
#
# The central architectural guarantee: whatever the preview endpoint
# returns for a given (source, partner, exams) is what the final
# create path will snapshot into the persisted items. Both code paths
# go through the same ``RequestPricingResolver``, so a drift can only
# appear if someone introduces a shortcut. These tests stop that.
# ---------------------------------------------------------------------------

class TestPreviewMatchesFinal:

    def test_direct_patient_preview_matches_persisted_items(
        self, admin_client, patient, exam, exam_b, lab_admin, make_request,
    ):
        exam_ids = [str(exam.id), str(exam_b.id)]

        preview = _data(admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.DIRECT_PATIENT,
                'exam_definition_ids': exam_ids,
            },
            format='json',
        ))['items']

        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': e.id} for e in [exam, exam_b]],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        preview_by_code = {p['exam_code']: p for p in preview}
        persisted_by_code = {i.exam_definition.code: i for i in ar.items.all()}
        for code in ('GLU', 'HBA1C'):
            assert Decimal(preview_by_code[code]['unit_price']) == persisted_by_code[code].unit_price
            assert Decimal(preview_by_code[code]['billed_price']) == persisted_by_code[code].billed_price
            assert preview_by_code[code]['price_source'] == persisted_by_code[code].price_source

    def test_partner_preview_matches_persisted_items(
        self, admin_client, patient, exam, partner, lab_admin, make_request,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )

        preview = _data(admin_client.post(
            f'{API}/preview-pricing/',
            {
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': str(partner.id),
                'exam_definition_ids': [str(exam.id)],
            },
            format='json',
        ))['items'][0]

        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner.id,
                'billing_mode': BillingMode.PARTNER_BILLING,
                'items': [{'exam_definition_id': exam.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        persisted = ar.items.first()

        assert Decimal(preview['unit_price']) == persisted.unit_price
        assert Decimal(preview['billed_price']) == persisted.billed_price
        assert preview['price_source'] == persisted.price_source
        assert persisted.billed_price == Decimal('35.0000')
