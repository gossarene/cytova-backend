"""
Tests for partner invoicing.

Covers:
- Partner discount rate configuration
- Preview computes correct totals
- Generation creates invoice + snapshotted lines
- Negotiated exam prices are reflected in invoice lines
- Discount applied correctly on gross total
- Invoice numbering format and uniqueness
- Confirmed invoice blocks duplicate same-partner/same-period
- Status transitions (confirm, cancel)
- Audit entries written on generate and confirm
- Already-invoiced items excluded from new invoices
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.invoicing.models import Invoice, InvoiceLine, InvoiceStatus
from apps.invoicing.services import InvoiceService
from apps.partners.models import PartnerExamPrice, PartnerOrganization
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


API = '/api/v1/invoicing'

pytestmark = pytest.mark.no_auto_labels


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
        code='CLINIC-INV',
        name='Invoice Test Clinic',
        organization_type='CLINIC',
        invoice_discount_rate=Decimal('10.00'),
    )


@pytest.fixture()
def partner_no_discount():
    return PartnerOrganization.objects.create(
        code='CLINIC-ND',
        name='No Discount Clinic',
        organization_type='CLINIC',
    )


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-INV-001',
        first_name='Jean', last_name='Facture',
        date_of_birth=date(1980, 1, 1), gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Biochemistry', display_order=1)


@pytest.fixture()
def exam_a(category, family, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='GLU-INV', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70-100',
        unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def exam_b(category, family, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='CRP-INV', name='C-Reactive Protein',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/L', reference_range='0-5',
        unit_price=Decimal('30.0000'),
    )


def _finalize_partner_request(
    patient, lab_admin, technician, biologist, make_request, partner, exams,
):
    """Create + confirm + collect + enter + validate + finalize a partner request."""
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

    req_tech = make_request(technician)
    req_bio = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req_tech,
        )
    for item in ar.items.select_related('exam_definition').all():
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_tech,
            result_value='42',
            values=[{'value': '42', 'is_abnormal': False}],
            comments='',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_tech)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(
            version=v, validation_notes='OK', validated_by=biologist,
            request=req_bio,
        )
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(
        analysis_request=ar, finalized_by=biologist, request=req_bio,
    )
    ar.refresh_from_db()
    return ar


@pytest.fixture()
def finalized_request(
    patient, lab_admin, technician, biologist, make_request, partner, exam_a, exam_b,
):
    return _finalize_partner_request(
        patient, lab_admin, technician, biologist, make_request, partner,
        [exam_a, exam_b],
    )


# ---------------------------------------------------------------------------
# Partner discount configuration
# ---------------------------------------------------------------------------

class TestPartnerDiscount:

    def test_discount_rate_stored(self, partner):
        assert partner.invoice_discount_rate == Decimal('10.00')

    def test_no_discount_is_null(self, partner_no_discount):
        assert partner_no_discount.invoice_discount_rate is None


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

class TestPreview:

    def test_preview_computes_totals(
        self, finalized_request, partner, make_request, lab_admin,
    ):
        today = finalized_request.confirmed_at.date()
        result = InvoiceService.preview(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        assert result['line_count'] == 2
        gross = Decimal('50.00') + Decimal('30.00')
        assert result['gross_total'] == gross
        assert result['discount_rate'] == Decimal('10.00')
        expected_discount = (gross * Decimal('10') / Decimal('100')).quantize(Decimal('0.01'))
        assert result['discount_amount'] == expected_discount
        subtotal = gross - expected_discount
        assert result['subtotal_after_discount'] == subtotal
        assert result['vat_rate'] == Decimal('0')
        assert result['vat_amount'] == Decimal('0')
        assert result['net_total'] == subtotal

    def test_preview_with_negotiated_price(
        self, patient, lab_admin, technician, biologist, make_request,
        partner, exam_a,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam_a,
            agreed_price=Decimal('35.0000'), is_active=True,
        )
        _finalize_partner_request(
            patient, lab_admin, technician, biologist, make_request,
            partner, [exam_a],
        )
        today = date.today()
        result = InvoiceService.preview(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        assert any(
            ld['billed_price_snapshot'] == Decimal('35.0000')
            for ld in result['lines']
        )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class TestGeneration:

    def test_generates_draft_invoice_with_lines(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        today = finalized_request.confirmed_at.date()
        invoice = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert invoice.status == InvoiceStatus.DRAFT
        assert invoice.lines.count() == 2
        assert invoice.gross_total == Decimal('80.00')
        assert invoice.discount_rate_snapshot == Decimal('10.00')

    def test_invoice_number_format(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        today = finalized_request.confirmed_at.date()
        invoice = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert invoice.invoice_number.startswith('INV-')
        assert len(invoice.invoice_number) == 17  # INV-YYYYMM-NNNNNN

    def test_line_snapshots_are_frozen(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        today = finalized_request.confirmed_at.date()
        invoice = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        for line in invoice.lines.all():
            assert line.patient_display_name
            assert line.exam_code_snapshot
            assert line.request_number_snapshot
            assert line.line_amount > 0

    def test_no_items_raises(self, partner, lab_admin, make_request):
        with pytest.raises(ValidationError):
            InvoiceService.generate(
                partner=partner,
                period_start=date(2000, 1, 1),
                period_end=date(2000, 1, 31),
                generated_by=lab_admin,
                request=make_request(lab_admin),
            )

    def test_audit_entry_written(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        today = finalized_request.confirmed_at.date()
        before = AuditLog.objects.filter(
            entity_type='Invoice', action='CREATE',
        ).count()
        InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        after = AuditLog.objects.filter(
            entity_type='Invoice', action='CREATE',
        ).count()
        assert after == before + 1


# ---------------------------------------------------------------------------
# Confirmation + duplicate prevention
# ---------------------------------------------------------------------------

class TestConfirmation:

    def _gen(self, finalized_request, partner, lab_admin, make_request):
        today = finalized_request.confirmed_at.date()
        return InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )

    def test_confirm_locks_invoice(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        invoice = self._gen(finalized_request, partner, lab_admin, make_request)
        confirmed = InvoiceService.confirm(
            invoice=invoice,
            confirmed_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert confirmed.status == InvoiceStatus.CONFIRMED
        assert confirmed.confirmed_at is not None

    def test_duplicate_confirmed_blocked(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        inv1 = self._gen(finalized_request, partner, lab_admin, make_request)
        InvoiceService.confirm(inv1, lab_admin, make_request(lab_admin))

        with pytest.raises(ValidationError, match='confirmed invoice already exists'):
            inv2 = self._gen(finalized_request, partner, lab_admin, make_request)
            InvoiceService.confirm(inv2, lab_admin, make_request(lab_admin))

    def test_confirmed_items_excluded_from_next_invoice(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        today = finalized_request.confirmed_at.date()
        inv = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        InvoiceService.confirm(inv, lab_admin, make_request(lab_admin))

        result = InvoiceService.preview(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        assert result['line_count'] == 0

    def test_confirm_audit_entry(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        inv = self._gen(finalized_request, partner, lab_admin, make_request)
        before = AuditLog.objects.filter(
            entity_type='Invoice', action='CONFIRM',
        ).count()
        InvoiceService.confirm(inv, lab_admin, make_request(lab_admin))
        after = AuditLog.objects.filter(
            entity_type='Invoice', action='CONFIRM',
        ).count()
        assert after == before + 1


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class TestCancellation:

    def test_cancel_voids_draft(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        today = finalized_request.confirmed_at.date()
        inv = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        cancelled = InvoiceService.cancel(inv, lab_admin, make_request(lab_admin))
        assert cancelled.status == InvoiceStatus.CANCELLED

    def test_cannot_cancel_confirmed(
        self, finalized_request, partner, lab_admin, make_request,
    ):
        today = finalized_request.confirmed_at.date()
        inv = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        InvoiceService.confirm(inv, lab_admin, make_request(lab_admin))
        with pytest.raises(ValidationError):
            InvoiceService.cancel(inv, lab_admin, make_request(lab_admin))


# ---------------------------------------------------------------------------
# HTTP endpoints — list + detail
# ---------------------------------------------------------------------------

class TestListEndpoint:

    @pytest.fixture()
    def client(self, lab_admin):
        c = APIClient(HTTP_HOST='testlab.localhost')
        c.force_authenticate(user=lab_admin)
        return c

    def _gen(self, finalized_request, partner, lab_admin, make_request):
        today = finalized_request.confirmed_at.date()
        return InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )

    def test_list_empty(self, client):
        resp = client.get(f'{API}/')
        assert resp.status_code == 200
        body = resp.json()
        data = body.get('data', body)
        assert isinstance(data, list)
        assert len(data) == 0

    def test_list_with_draft(
        self, client, finalized_request, partner, lab_admin, make_request,
    ):
        self._gen(finalized_request, partner, lab_admin, make_request)
        resp = client.get(f'{API}/')
        assert resp.status_code == 200
        data = resp.json().get('data', resp.json())
        assert len(data) == 1
        assert data[0]['status'] == 'DRAFT'
        assert data[0]['invoice_number'].startswith('INV-')
        assert data[0]['partner_name'] == partner.name

    def test_list_with_confirmed(
        self, client, finalized_request, partner, lab_admin, make_request,
    ):
        inv = self._gen(finalized_request, partner, lab_admin, make_request)
        InvoiceService.confirm(inv, lab_admin, make_request(lab_admin))
        resp = client.get(f'{API}/')
        data = resp.json().get('data', resp.json())
        assert data[0]['status'] == 'CONFIRMED'

    def test_list_with_cancelled(
        self, client, finalized_request, partner, lab_admin, make_request,
    ):
        inv = self._gen(finalized_request, partner, lab_admin, make_request)
        InvoiceService.cancel(inv, lab_admin, make_request(lab_admin))
        resp = client.get(f'{API}/')
        data = resp.json().get('data', resp.json())
        assert data[0]['status'] == 'CANCELLED'

    def test_list_mixed_statuses(
        self, client, finalized_request, partner, lab_admin, make_request,
        patient, exam_a, technician, biologist,
    ):
        inv1 = self._gen(finalized_request, partner, lab_admin, make_request)
        InvoiceService.confirm(inv1, lab_admin, make_request(lab_admin))
        # Create a second request for a different period
        ar2 = _finalize_partner_request(
            patient, lab_admin, technician, biologist, make_request,
            partner, [exam_a],
        )
        today = ar2.confirmed_at.date()
        inv2 = InvoiceService.generate(
            partner=partner,
            period_start=today - timedelta(days=30),
            period_end=today + timedelta(days=30),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        resp = client.get(f'{API}/')
        data = resp.json().get('data', resp.json())
        assert len(data) == 2
        statuses = {d['status'] for d in data}
        assert 'CONFIRMED' in statuses
        assert 'DRAFT' in statuses

    def test_detail_returns_lines(
        self, client, finalized_request, partner, lab_admin, make_request,
    ):
        inv = self._gen(finalized_request, partner, lab_admin, make_request)
        resp = client.get(f'{API}/{inv.id}/')
        assert resp.status_code == 200
        data = resp.json().get('data', resp.json())
        assert data['invoice_number'] == inv.invoice_number
        assert len(data['lines']) == 2

    def test_unauthenticated_blocked(self):
        c = APIClient(HTTP_HOST='testlab.localhost')
        resp = c.get(f'{API}/')
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# VAT + discount financial calculations
# ---------------------------------------------------------------------------

class TestFinancialCalculations:

    def _set_vat(self, rate):
        from apps.lab_settings.models import LabSettings
        lab = LabSettings.get_solo()
        lab.default_invoice_vat_rate = rate
        lab.save(update_fields=['default_invoice_vat_rate', 'updated_at'])

    def test_vat_only(
        self, patient, lab_admin, technician, biologist, make_request, exam_a,
    ):
        self._set_vat(Decimal('18.00'))
        partner_vat = PartnerOrganization.objects.create(
            code='CLINIC-VAT', name='VAT Clinic',
            organization_type='CLINIC',
        )
        ar = _finalize_partner_request(
            patient, lab_admin, technician, biologist, make_request,
            partner_vat, [exam_a],
        )
        today = ar.confirmed_at.date()
        result = InvoiceService.preview(
            partner=partner_vat,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        gross = Decimal('50.00')
        assert result['gross_total'] == gross
        assert result['discount_rate'] == Decimal('0')
        assert result['discount_amount'] == Decimal('0')
        assert result['subtotal_after_discount'] == gross
        vat = (gross * Decimal('18') / Decimal('100')).quantize(Decimal('0.01'))
        assert result['vat_amount'] == vat
        assert result['net_total'] == gross + vat

    def test_discount_plus_vat(
        self, patient, lab_admin, technician, biologist, make_request, exam_a,
    ):
        self._set_vat(Decimal('18.00'))
        partner_both = PartnerOrganization.objects.create(
            code='CLINIC-BOTH', name='Both Clinic',
            organization_type='CLINIC',
            invoice_discount_rate=Decimal('10.00'),
        )
        ar = _finalize_partner_request(
            patient, lab_admin, technician, biologist, make_request,
            partner_both, [exam_a],
        )
        today = ar.confirmed_at.date()
        result = InvoiceService.preview(
            partner=partner_both,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        gross = Decimal('50.00')
        discount = (gross * Decimal('10') / Decimal('100')).quantize(Decimal('0.01'))
        subtotal = gross - discount
        vat = (subtotal * Decimal('18') / Decimal('100')).quantize(Decimal('0.01'))
        assert result['gross_total'] == gross
        assert result['discount_amount'] == discount
        assert result['subtotal_after_discount'] == subtotal
        assert result['vat_amount'] == vat
        assert result['net_total'] == subtotal + vat

    def test_null_rates_treated_as_zero(
        self, patient, lab_admin, technician, biologist, make_request,
        partner_no_discount, exam_a,
    ):
        self._set_vat(None)
        ar = _finalize_partner_request(
            patient, lab_admin, technician, biologist, make_request,
            partner_no_discount, [exam_a],
        )
        today = ar.confirmed_at.date()
        result = InvoiceService.preview(
            partner=partner_no_discount,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        gross = Decimal('50.00')
        assert result['discount_amount'] == Decimal('0')
        assert result['vat_amount'] == Decimal('0')
        assert result['net_total'] == gross

    def test_generated_invoice_snapshots_all_totals(
        self, patient, lab_admin, technician, biologist, make_request, exam_a,
    ):
        self._set_vat(Decimal('20.00'))
        partner_snap = PartnerOrganization.objects.create(
            code='CLINIC-SNAP', name='Snap Clinic',
            organization_type='CLINIC',
            invoice_discount_rate=Decimal('5.00'),
        )
        ar = _finalize_partner_request(
            patient, lab_admin, technician, biologist, make_request,
            partner_snap, [exam_a],
        )
        today = ar.confirmed_at.date()
        invoice = InvoiceService.generate(
            partner=partner_snap,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        gross = Decimal('50.00')
        discount = (gross * Decimal('5') / Decimal('100')).quantize(Decimal('0.01'))
        subtotal = gross - discount
        vat = (subtotal * Decimal('20') / Decimal('100')).quantize(Decimal('0.01'))
        assert invoice.gross_total == gross
        assert invoice.discount_rate_snapshot == Decimal('5.00')
        assert invoice.discount_amount == discount
        assert invoice.subtotal_after_discount == subtotal
        assert invoice.vat_rate_snapshot == Decimal('20.00')
        assert invoice.vat_amount == vat
        assert invoice.net_total == subtotal + vat

    def test_preview_matches_generated(
        self, patient, lab_admin, technician, biologist, make_request, exam_a,
    ):
        self._set_vat(Decimal('10.00'))
        partner_match = PartnerOrganization.objects.create(
            code='CLINIC-MATCH', name='Match Clinic',
            organization_type='CLINIC',
            invoice_discount_rate=Decimal('15.00'),
        )
        ar = _finalize_partner_request(
            patient, lab_admin, technician, biologist, make_request,
            partner_match, [exam_a],
        )
        today = ar.confirmed_at.date()
        preview = InvoiceService.preview(
            partner=partner_match,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
        )
        invoice = InvoiceService.generate(
            partner=partner_match,
            period_start=today - timedelta(days=1),
            period_end=today + timedelta(days=1),
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert invoice.gross_total == preview['gross_total']
        assert invoice.discount_amount == preview['discount_amount']
        assert invoice.subtotal_after_discount == preview['subtotal_after_discount']
        assert invoice.vat_amount == preview['vat_amount']
        assert invoice.net_total == preview['net_total']
