"""
Tests for dual document mode: Invoice PDF vs Financial Statement PDF.

Covers:
- Invoice mode renders title "INVOICE" + invoice number
- Statement mode renders title "FINANCIAL STATEMENT", no invoice number
- Statement mode omits Status and Partner code
- Lab settings controls available document types
- Both PDFs can coexist on the same invoice
- Secure download works for both types
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.storage import default_storage
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.invoicing.models import Invoice
from apps.invoicing.pdf_service import (
    DOC_INVOICE, DOC_STATEMENT, InvoicePdfService,
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

API = '/api/v1/invoicing'
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
def partner():
    return PartnerOrganization.objects.create(
        code='CLINIC-DOC', name='Doc Clinic',
        organization_type='CLINIC',
        invoice_discount_rate=Decimal('5.00'),
    )


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-DOC-001',
        first_name='Doc', last_name='Test',
        date_of_birth=date(1990, 1, 1), gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='L', display_order=1)
    fam = ExamFamily.objects.create(name='B', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='DOC', name='Doc Exam',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('100.0000'),
    )


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


@pytest.fixture()
def invoice(patient, exam, partner, lab_admin, technician, biologist, make_request):
    ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, exam)
    return InvoiceService.generate(
        partner=partner,
        period_start=ar.confirmed_at.date() - timedelta(days=1),
        period_end=ar.confirmed_at.date() + timedelta(days=1),
        generated_by=lab_admin,
        request=make_request(lab_admin),
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


class TestInvoicePdfContent:

    def test_invoice_mode_renders_title_and_number(self, invoice, monkeypatch):
        from apps.invoicing import pdf_service
        from reportlab.pdfgen.canvas import Canvas

        drawn = []
        orig = Canvas.drawString

        def spy(self, x, y, text, *a, **kw):
            drawn.append(text)
            return orig(self, x, y, text, *a, **kw)

        monkeypatch.setattr(Canvas, 'drawString', spy)
        InvoicePdfService.regenerate(invoice, DOC_INVOICE)

        assert 'INVOICE' in drawn
        assert invoice.invoice_number in drawn

    def test_statement_mode_renders_statement_title(self, invoice, monkeypatch):
        from reportlab.pdfgen.canvas import Canvas

        drawn = []
        orig = Canvas.drawString

        def spy(self, x, y, text, *a, **kw):
            drawn.append(text)
            return orig(self, x, y, text, *a, **kw)

        monkeypatch.setattr(Canvas, 'drawString', spy)
        InvoicePdfService.regenerate(invoice, DOC_STATEMENT)

        assert 'FINANCIAL STATEMENT' in drawn
        assert invoice.invoice_number not in drawn

    def test_statement_omits_status_and_partner_code(self, invoice, monkeypatch):
        from reportlab.pdfgen.canvas import Canvas

        drawn = []
        orig = Canvas.drawString

        def spy(self, x, y, text, *a, **kw):
            drawn.append(text)
            return orig(self, x, y, text, *a, **kw)

        monkeypatch.setattr(Canvas, 'drawString', spy)
        InvoicePdfService.regenerate(invoice, DOC_STATEMENT)

        assert 'Status' not in drawn
        assert 'Partner code' not in drawn
        assert invoice.partner.code not in drawn


class TestDualDocumentPersistence:

    def test_both_pdfs_coexist(self, invoice):
        InvoicePdfService.generate_or_get(invoice, DOC_INVOICE)
        InvoicePdfService.generate_or_get(invoice, DOC_STATEMENT)
        invoice.refresh_from_db()
        assert invoice.pdf_file_key
        assert invoice.statement_file_key
        assert invoice.pdf_file_key != invoice.statement_file_key

    def test_each_is_valid_pdf(self, invoice):
        InvoicePdfService.generate_or_get(invoice, DOC_INVOICE)
        InvoicePdfService.generate_or_get(invoice, DOC_STATEMENT)
        invoice.refresh_from_db()
        for key in (invoice.pdf_file_key, invoice.statement_file_key):
            with default_storage.open(key, 'rb') as f:
                assert f.read(5) == b'%PDF-'


class TestLabSettingsDocumentMode:

    def test_default_is_invoice_only(self):
        lab = LabSettings.get_solo()
        assert lab.financial_document_mode == 'INVOICE_ONLY'


class TestHttpEndpoints:

    @pytest.fixture()
    def client(self, lab_admin):
        c = APIClient(HTTP_HOST='testlab.localhost')
        c.force_authenticate(user=lab_admin)
        return c

    def test_generate_statement_endpoint(self, client, invoice):
        resp = client.post(f'{API}/{invoice.id}/generate-statement/')
        assert resp.status_code == 200
        assert _data(resp)['has_statement'] is True
        assert _data(resp)['statement_url'] is not None

    def test_download_statement(self, client, invoice):
        client.post(f'{API}/{invoice.id}/generate-statement/')
        resp = client.get(f'{API}/{invoice.id}/download-statement/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'

    def test_download_statement_404_without_generation(self, client, invoice):
        resp = client.get(f'{API}/{invoice.id}/download-statement/')
        assert resp.status_code == 404

    def test_detail_exposes_both_pdf_states(self, client, invoice):
        client.post(f'{API}/{invoice.id}/generate-pdf/')
        client.post(f'{API}/{invoice.id}/generate-statement/')
        resp = client.get(f'{API}/{invoice.id}/')
        body = _data(resp)
        assert body['has_pdf'] is True
        assert body['has_statement'] is True
        assert body['pdf_url'] is not None
        assert body['statement_url'] is not None
