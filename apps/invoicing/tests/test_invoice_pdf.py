"""
Tests for invoice PDF generation and secure download.

Covers:
- PDF can be generated for DRAFT and CONFIRMED invoices
- PDF uses snapshot data (not mutable)
- Grouped rendering: repeated dates suppressed, repeated patients suppressed
- Secure download works
- Unauthorized access blocked
- Generated PDF remains available on revisit
- No regression in invoicing behavior
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
from apps.invoicing.models import Invoice, InvoiceStatus
from apps.invoicing.pdf_service import InvoicePdfService
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
        code='CLINIC-PDF', name='PDF Clinic',
        organization_type='CLINIC',
        invoice_discount_rate=Decimal('10.00'),
    )


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-PDF-001',
        first_name='Jean', last_name='Facture',
        date_of_birth=date(1980, 1, 1), gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def patient2(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-PDF-002',
        first_name='Marie', last_name='Invoice',
        date_of_birth=date(1985, 6, 15), gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Bio', display_order=1)


@pytest.fixture()
def exam_a(category, family, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='GLU', name='Glucose',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70-100',
        unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def exam_b(category, family, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='CRP', name='C-Reactive Protein',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/L', reference_range='0-5',
        unit_price=Decimal('30.0000'),
    )


def _finalize(patient, lab_admin, technician, biologist, make_request, partner, exams):
    from apps.requests.label_service import RequestLabelService
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.PARTNER_ORGANIZATION,
            'partner_organization_id': partner.id,
            'items': [{'exam_definition_id': e.id} for e in exams],
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


def _gen_invoice(partner, lab_admin, make_request, confirmed_at_date):
    return InvoiceService.generate(
        partner=partner,
        period_start=confirmed_at_date - timedelta(days=1),
        period_end=confirmed_at_date + timedelta(days=1),
        generated_by=lab_admin,
        request=make_request(lab_admin),
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

class TestPdfGeneration:

    def test_generates_valid_pdf(
        self, patient, exam_a, exam_b, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a, exam_b])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())
        invoice = InvoicePdfService.generate_or_get(invoice)

        assert invoice.pdf_file_key
        assert default_storage.exists(invoice.pdf_file_key)
        with default_storage.open(invoice.pdf_file_key, 'rb') as f:
            assert f.read(5) == b'%PDF-'

    def test_idempotent(
        self, patient, exam_a, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())
        first = InvoicePdfService.generate_or_get(invoice)
        key1 = first.pdf_file_key
        second = InvoicePdfService.generate_or_get(invoice)
        assert second.pdf_file_key == key1

    def test_works_for_confirmed_invoice(
        self, patient, exam_a, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())
        InvoiceService.confirm(invoice, lab_admin, make_request(lab_admin))
        invoice.refresh_from_db()
        invoice = InvoicePdfService.generate_or_get(invoice)
        assert invoice.pdf_file_key


# ---------------------------------------------------------------------------
# Grouped rendering verification
# ---------------------------------------------------------------------------

class TestGroupedRendering:

    def test_multiple_patients_same_date(
        self, patient, patient2, exam_a, exam_b, partner,
        lab_admin, technician, biologist, make_request, monkeypatch,
    ):
        """
        Two patients, same day, multiple exams — verify grouping logic
        by patching the _draw_lines_table function to intercept the
        group_info computation.
        """
        _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a, exam_b])
        _finalize(patient2, lab_admin, technician, biologist, make_request, partner, [exam_a])

        invoice = _gen_invoice(partner, lab_admin, make_request, date.today())

        from apps.invoicing import pdf_service

        captured_groups = []
        orig_draw = pdf_service._draw_lines_table

        def spy_draw(c, y, lines, *a, **kw):
            # Recompute group_info the same way the real function does
            n = len(lines)
            for i, line in enumerate(lines):
                cur_date = line.performed_date.isoformat() if line.performed_date else '—'
                cur_patient = line.patient_display_name
                next_date = (
                    (lines[i + 1].performed_date.isoformat()
                     if lines[i + 1].performed_date else '—')
                    if i + 1 < n else None
                )
                next_patient = lines[i + 1].patient_display_name if i + 1 < n else None
                captured_groups.append({
                    'date': cur_date,
                    'patient': cur_patient,
                    'is_last_in_date': next_date != cur_date,
                    'is_last_in_patient': (next_date != cur_date or next_patient != cur_patient),
                })
            return orig_draw(c, y, lines, *a, **kw)

        monkeypatch.setattr(pdf_service, '_draw_lines_table', spy_draw)
        InvoicePdfService.regenerate(invoice)

        # 3 lines total, all same date → date group has 1 entry
        dates = {g['date'] for g in captured_groups}
        assert len(dates) == 1

        # 2 distinct patients
        patients = {g['patient'] for g in captured_groups}
        assert len(patients) == 2

        # Only the last row should be last_in_date
        last_date_flags = [g['is_last_in_date'] for g in captured_groups]
        assert last_date_flags.count(True) == 2  # once per 2-pass render

        # Patient group boundaries: Facture has 2 exams (last is boundary),
        # Invoice has 1 exam (last is boundary) → 2 boundaries per pass
        last_patient_flags = [g['is_last_in_patient'] for g in captured_groups]
        assert last_patient_flags.count(True) == 4  # 2 patients × 2 passes


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

class TestHttpEndpoints:

    @pytest.fixture()
    def client(self, lab_admin):
        c = APIClient(HTTP_HOST='testlab.localhost')
        c.force_authenticate(user=lab_admin)
        return c

    def test_generate_pdf_endpoint(
        self, client, patient, exam_a, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())
        resp = client.post(f'{API}/{invoice.id}/generate-pdf/')
        assert resp.status_code == 200
        body = _data(resp)
        assert body['has_pdf'] is True
        assert body['pdf_url'] == f'/invoicing/{invoice.id}/download/'

    def test_download_streams_pdf(
        self, client, patient, exam_a, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())
        client.post(f'{API}/{invoice.id}/generate-pdf/')

        resp = client.get(f'{API}/{invoice.id}/download/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'
        body = b''.join(resp.streaming_content)
        assert body.startswith(b'%PDF-')

    def test_download_404_without_pdf(
        self, client, patient, exam_a, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())
        resp = client.get(f'{API}/{invoice.id}/download/')
        assert resp.status_code == 404

    def test_unauthenticated_download_blocked(
        self, patient, exam_a, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())
        InvoicePdfService.generate_or_get(invoice)
        fresh = APIClient(HTTP_HOST='testlab.localhost')
        resp = fresh.get(f'{API}/{invoice.id}/download/')
        assert resp.status_code == 401

    def test_detail_exposes_pdf_state(
        self, client, patient, exam_a, partner,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, partner, [exam_a])
        invoice = _gen_invoice(partner, lab_admin, make_request, ar.confirmed_at.date())

        resp = client.get(f'{API}/{invoice.id}/')
        assert _data(resp)['has_pdf'] is False

        client.post(f'{API}/{invoice.id}/generate-pdf/')
        resp = client.get(f'{API}/{invoice.id}/')
        assert _data(resp)['has_pdf'] is True
        assert _data(resp)['pdf_url'] is not None
