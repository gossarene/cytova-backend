"""
Tests for the Financial Reports preview + export endpoints.

Coverage:
  - preview shape (summary, rows, charts, filters_applied)
  - source filters: ALL, DIRECT_PATIENT, PARTNER (+ no/single/multiple ids)
  - totals are correct (gross/discount/net) including partner discount
  - export returns a PDF and never creates an Invoice record
  - tenant isolation handled by the autouse ``_in_tenant_schema`` fixture

These exercise the HTTP layer end-to-end through the DRF APIClient so
the URL conf, permission gate, and composer are validated together.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.invoicing.models import Invoice
from apps.partners.models import PartnerOrganization
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


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


def _client(user) -> APIClient:
    client = APIClient()
    token = RefreshToken.for_user(user).access_token
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _preview(user, payload) -> dict:
    resp = _client(user).post(
        '/api/v1/financial-reports/preview/',
        data=payload, format='json',
        HTTP_HOST='testlab.localhost',
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body.get('data', body)


def _export(user, payload):
    return _client(user).post(
        '/api/v1/financial-reports/export/',
        data=payload, format='json',
        HTTP_HOST='testlab.localhost',
    )


# ---------------------------------------------------------------------------
# Fixtures — minimal billable workflow
# ---------------------------------------------------------------------------

@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-FR-001',
        first_name='Frank', last_name='Reports',
        date_of_birth=date(1990, 1, 1), gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='FRcat', display_order=1)
    fam = ExamFamily.objects.create(name='FRfam', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='FR-CBC', name='FR CBC',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('100.0000'),
    )


@pytest.fixture()
def partner_a():
    return PartnerOrganization.objects.create(
        code='FR-PA', name='Partner A', organization_type='CLINIC',
        invoice_discount_rate=Decimal('10.00'),
    )


@pytest.fixture()
def partner_b():
    return PartnerOrganization.objects.create(
        code='FR-PB', name='Partner B', organization_type='HOSPITAL',
        invoice_discount_rate=Decimal('0.00'),
    )


def _finalize(ar, lab_admin, technician, biologist, make_request):
    from apps.requests.label_service import RequestLabelService
    RequestLabelService.generate_or_get(ar, lab_admin, make_request(lab_admin))
    req_t = make_request(technician)
    req_b = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req_t,
        )
    for item in ar.items.select_related('exam_definition').all():
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_t,
            result_value='42',
            values=[{'value': '42', 'is_abnormal': False}], comments='',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(
            version=v, validation_notes='OK',
            validated_by=biologist, request=req_b,
        )
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(
        analysis_request=ar, finalized_by=biologist, request=req_b,
    )
    ar.refresh_from_db()
    return ar


def _make_partner_request(lab_admin, technician, biologist, make_request, patient, partner, exam):
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
    return _finalize(ar, lab_admin, technician, biologist, make_request)


def _make_direct_request(lab_admin, technician, biologist, make_request, patient, exam):
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )
    return _finalize(ar, lab_admin, technician, biologist, make_request)


@pytest.fixture()
def billable_data(
    lab_admin, technician, biologist, make_request,
    patient, exam, partner_a, partner_b,
):
    """Three validated requests:
       - 1 direct patient (gross 100, no discount, net 100)
       - 1 partner_a (gross 100, 10% discount → net 90)
       - 1 partner_b (gross 100, 0% discount → net 100)
    """
    direct = _make_direct_request(
        lab_admin, technician, biologist, make_request, patient, exam,
    )
    pa = _make_partner_request(
        lab_admin, technician, biologist, make_request,
        patient, partner_a, exam,
    )
    pb = _make_partner_request(
        lab_admin, technician, biologist, make_request,
        patient, partner_b, exam,
    )
    return {'direct': direct, 'partner_a': pa, 'partner_b': pb}


# ---------------------------------------------------------------------------
# Shape + role gating
# ---------------------------------------------------------------------------

def _payload(source_type='ALL', partner_ids=None):
    today = date.today()
    return {
        'period_start': (today - timedelta(days=7)).isoformat(),
        'period_end':   (today + timedelta(days=1)).isoformat(),
        'source_type':  source_type,
        'partner_ids':  partner_ids or [],
    }


@pytest.mark.django_db(transaction=True)
class TestPreviewShape:

    def test_top_level_keys(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload())
        assert set(body.keys()) >= {
            'summary', 'rows', 'charts', 'filters_applied',
        }
        assert set(body['summary'].keys()) >= {
            'request_count', 'exam_count',
            'gross_total', 'discount_total', 'net_total',
        }
        assert set(body['charts'].keys()) >= {
            'source_distribution', 'time_evolution',
            'top_exams_by_revenue', 'top_exams_by_volume',
            'top_partners_by_revenue', 'partner_time_comparison',
        }


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestFilters:

    def test_all_includes_every_validated_request(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload(source_type='ALL'))
        assert body['summary']['request_count'] == 3

    def test_direct_patient_only(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload(source_type='DIRECT_PATIENT'))
        assert body['summary']['request_count'] == 1
        assert all(r['source_type'] == 'DIRECT_PATIENT' for r in body['rows'])

    def test_partner_no_ids_includes_all_partners(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload(source_type='PARTNER'))
        assert body['summary']['request_count'] == 2
        assert all(r['source_type'] == 'PARTNER_ORGANIZATION' for r in body['rows'])

    def test_partner_single_id(self, billable_data, lab_admin, partner_a):
        body = _preview(lab_admin, _payload(
            source_type='PARTNER',
            partner_ids=[str(partner_a.id)],
        ))
        assert body['summary']['request_count'] == 1
        assert body['rows'][0]['partner_id'] == str(partner_a.id)

    def test_partner_multiple_ids(self, billable_data, lab_admin, partner_a, partner_b):
        body = _preview(lab_admin, _payload(
            source_type='PARTNER',
            partner_ids=[str(partner_a.id), str(partner_b.id)],
        ))
        assert body['summary']['request_count'] == 2


# ---------------------------------------------------------------------------
# Totals — gross / discount / net
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestTotals:

    def test_partner_a_discount_applied(self, billable_data, lab_admin, partner_a):
        body = _preview(lab_admin, _payload(
            source_type='PARTNER', partner_ids=[str(partner_a.id)],
        ))
        s = body['summary']
        # gross 100, 10% discount → 10 discount, net 90
        assert Decimal(s['gross_total']) == Decimal('100.00')
        assert Decimal(s['discount_total']) == Decimal('10.00')
        assert Decimal(s['net_total']) == Decimal('90.00')

    def test_direct_patient_no_discount(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload(source_type='DIRECT_PATIENT'))
        s = body['summary']
        assert Decimal(s['gross_total']) == Decimal('100.00')
        assert Decimal(s['discount_total']) == Decimal('0.00')
        assert Decimal(s['net_total']) == Decimal('100.00')

    def test_all_aggregate(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload())
        s = body['summary']
        # 3 requests × 100 gross = 300; only partner_a applies 10 discount
        assert Decimal(s['gross_total']) == Decimal('300.00')
        assert Decimal(s['discount_total']) == Decimal('10.00')
        assert Decimal(s['net_total']) == Decimal('290.00')


# ---------------------------------------------------------------------------
# Charts — conditional rendering rules
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestChartRules:

    def test_top_partners_shown_for_all(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload(source_type='ALL'))
        assert len(body['charts']['top_partners_by_revenue']) == 2

    def test_top_partners_hidden_for_single_partner(
        self, billable_data, lab_admin, partner_a,
    ):
        body = _preview(lab_admin, _payload(
            source_type='PARTNER', partner_ids=[str(partner_a.id)],
        ))
        assert body['charts']['top_partners_by_revenue'] == []

    def test_top_partners_shown_for_multiple_partners(
        self, billable_data, lab_admin, partner_a, partner_b,
    ):
        body = _preview(lab_admin, _payload(
            source_type='PARTNER',
            partner_ids=[str(partner_a.id), str(partner_b.id)],
        ))
        assert len(body['charts']['top_partners_by_revenue']) == 2

    def test_partner_time_comparison_only_with_two_or_more(
        self, billable_data, lab_admin, partner_a, partner_b,
    ):
        # Single → comparison empty
        body = _preview(lab_admin, _payload(
            source_type='PARTNER', partner_ids=[str(partner_a.id)],
        ))
        assert body['charts']['partner_time_comparison'] == []
        # Two → comparison populated
        body = _preview(lab_admin, _payload(
            source_type='PARTNER',
            partner_ids=[str(partner_a.id), str(partner_b.id)],
        ))
        assert len(body['charts']['partner_time_comparison']) == 2


# ---------------------------------------------------------------------------
# Export — never creates an Invoice
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestDrillDown:

    def test_row_includes_exams_array(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload())
        for row in body['rows']:
            assert 'exams' in row
            assert isinstance(row['exams'], list)
            assert len(row['exams']) >= 1

    def test_exam_drill_down_shape(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload())
        exam = body['rows'][0]['exams'][0]
        assert {
            'code', 'name', 'quantity',
            'unit_price', 'gross_amount', 'discount_amount', 'net_amount',
        } <= set(exam.keys())

    def test_exam_amounts_sum_to_row(self, billable_data, lab_admin):
        body = _preview(lab_admin, _payload())
        for row in body['rows']:
            from decimal import Decimal
            row_gross = Decimal(row['gross_amount'])
            exam_total = sum(
                (Decimal(e['gross_amount']) for e in row['exams']),
                Decimal('0'),
            )
            assert row_gross == exam_total, row


@pytest.mark.django_db(transaction=True)
class TestExport:

    def test_export_returns_pdf(self, billable_data, lab_admin):
        before = Invoice.objects.count()
        resp = _export(lab_admin, _payload())
        assert resp.status_code == 200, resp.content
        assert resp['Content-Type'] == 'application/pdf'
        assert resp.content.startswith(b'%PDF')
        assert Invoice.objects.count() == before

    def test_export_does_not_create_invoice(self, billable_data, lab_admin):
        before = Invoice.objects.count()
        # Run twice — still no Invoice rows.
        _export(lab_admin, _payload())
        _export(lab_admin, _payload())
        assert Invoice.objects.count() == before
