"""
End-to-end verification that billing fields flow correctly:
- discount rate from PartnerOrganization
- VAT rate from LabSettings
- both snapshotted onto Invoice at generation time
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.invoicing.services import InvoiceService
from apps.lab_settings.models import LabSettings
from apps.partners.models import PartnerOrganization
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService

PARTNERS_API = '/api/v1/partners'

pytestmark = pytest.mark.no_auto_labels


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
                    'name': 'Test Trial', 'is_trial': True,
                    'trial_duration_days': 30, 'is_public': False,
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
def client(lab_admin):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=lab_admin)
    return c


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-E2E-001',
        first_name='E2E', last_name='Test',
        date_of_birth=date(1990, 1, 1), gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='L', display_order=1)
    fam = ExamFamily.objects.create(name='B', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='E2E', name='E2E Exam',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('100.0000'),
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


def _set_vat(rate):
    lab = LabSettings.get_solo()
    lab.default_invoice_vat_rate = rate
    lab.save(update_fields=['default_invoice_vat_rate', 'updated_at'])


def _finalize(patient, lab_admin, technician, biologist, make_request, partner, exam):
    from apps.requests.label_service import RequestLabelService
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.PARTNER_ORGANIZATION,
            'partner_organization_id': partner.id,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )
    RequestLabelService.generate_or_get(ar, lab_admin, make_request(lab_admin))
    req_t = make_request(technician)
    req_b = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(item=item, collected_by=technician, request=req_t)
    for item in ar.items.select_related('exam_definition').all():
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_t,
            result_value='42', values=[{'value': '42', 'is_abnormal': False}], comments='',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(version=v, validation_notes='OK', validated_by=biologist, request=req_b)
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(analysis_request=ar, finalized_by=biologist, request=req_b)
    ar.refresh_from_db()
    return ar


class TestEndToEndBillingFields:

    def test_partner_discount_via_api(self, client):
        """Partner create/update persists discount rate."""
        resp = client.post(f'{PARTNERS_API}/', {
            'code': 'E2E-DISC',
            'name': 'Discount Clinic',
            'organization_type': 'CLINIC',
            'invoice_discount_rate': '12.50',
        }, format='json')
        assert resp.status_code == 201
        assert _data(resp)['invoice_discount_rate'] == '12.50'

    def test_lab_vat_via_settings(self):
        """LabSettings stores the lab-level VAT rate."""
        _set_vat(Decimal('18.00'))
        lab = LabSettings.get_solo()
        assert lab.default_invoice_vat_rate == Decimal('18.00')

    def test_preview_uses_partner_discount_and_lab_vat(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        _set_vat(Decimal('18.00'))
        partner = PartnerOrganization.objects.create(
            code='E2E-PREV', name='Preview Clinic',
            organization_type='CLINIC',
            invoice_discount_rate=Decimal('10.00'),
        )
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, exam)
        today = ar.confirmed_at.date()

        result = InvoiceService.preview(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        gross = Decimal('100.00')
        discount = Decimal('10.00')
        subtotal = Decimal('90.00')
        vat = Decimal('16.20')
        assert result['gross_total'] == gross
        assert result['discount_rate'] == Decimal('10.00')
        assert result['discount_amount'] == discount
        assert result['subtotal_after_discount'] == subtotal
        assert result['vat_rate'] == Decimal('18.00')
        assert result['vat_amount'] == vat
        assert result['net_total'] == subtotal + vat

    def test_generation_snapshots_both_sources(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        _set_vat(Decimal('18.00'))
        partner = PartnerOrganization.objects.create(
            code='E2E-GEN', name='Gen Clinic',
            organization_type='CLINIC',
            invoice_discount_rate=Decimal('10.00'),
        )
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, exam)
        today = ar.confirmed_at.date()

        invoice = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert invoice.discount_rate_snapshot == Decimal('10.00')
        assert invoice.vat_rate_snapshot == Decimal('18.00')
        assert invoice.net_total == Decimal('106.20')

        # Changing lab VAT after generation must NOT affect the invoice
        _set_vat(Decimal('0'))
        invoice.refresh_from_db()
        assert invoice.vat_rate_snapshot == Decimal('18.00')
        assert invoice.vat_amount == Decimal('16.20')
