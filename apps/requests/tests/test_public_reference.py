"""
Tests for the patient-facing ``public_reference``.

Covers:
- Every new request receives a YYYYMMDD-NNNNNN public_reference.
- The daily sequence increments monotonically, scoped per-day.
- References are unique within the tenant schema.
- The internal ``request_number`` still exists in parallel.
- Final report PDF uses ``public_reference`` in the patient block.
- Detail serializer exposes both identifiers.
- Report download filename carries the public reference.
"""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest, RequestReferenceSequence, SourceType,
)
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
    _allocate_public_reference,
)


API = '/api/v1/requests'


# ---------------------------------------------------------------------------
# Subscription fixture — matches sibling test modules
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-PR-001',
        first_name='Peter', last_name='Public',
        date_of_birth=date(1980, 1, 1), gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family(category):
    return ExamFamily.objects.create(name='Biochemistry', display_order=1)


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Spectrophotometry')


@pytest.fixture()
def exam(category, family, technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=technique,
        code='GLU-PR', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70-100',
        unit_price=Decimal('50'),
    )


def _create(patient, lab_admin, make_request, exam_id):
    return AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam_id}],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Allocator level
# ---------------------------------------------------------------------------

class TestAllocator:

    def test_produces_expected_format(self, db):
        RequestReferenceSequence.objects.all().delete()
        ref = _allocate_public_reference(date(2026, 4, 16))
        assert ref == '20260416-000001'

    def test_increments_within_day(self, db):
        RequestReferenceSequence.objects.all().delete()
        a = _allocate_public_reference(date(2026, 4, 16))
        b = _allocate_public_reference(date(2026, 4, 16))
        c = _allocate_public_reference(date(2026, 4, 16))
        assert [a, b, c] == [
            '20260416-000001', '20260416-000002', '20260416-000003',
        ]

    def test_sequence_resets_per_day(self, db):
        RequestReferenceSequence.objects.all().delete()
        a = _allocate_public_reference(date(2026, 4, 16))
        b = _allocate_public_reference(date(2026, 4, 17))
        assert a == '20260416-000001'
        assert b == '20260417-000001'


# ---------------------------------------------------------------------------
# Service integration
# ---------------------------------------------------------------------------

class TestServiceIntegration:

    def test_create_assigns_both_identifiers(
        self, patient, exam, lab_admin, make_request,
    ):
        ar = _create(patient, lab_admin, make_request, exam.id)
        ar.refresh_from_db()
        # Internal identifier keeps REQ-YYYY-XXXXXXXX shape
        assert ar.request_number.startswith('REQ-')
        # Public reference is YYYYMMDD-NNNNNN
        assert len(ar.public_reference) == 15
        assert ar.public_reference[:8] == ar.created_at.strftime('%Y%m%d')
        assert ar.public_reference[8] == '-'
        assert ar.public_reference[9:].isdigit()

    def test_references_unique_across_creates(
        self, patient, exam, lab_admin, make_request,
    ):
        refs = set()
        for _ in range(5):
            ar = _create(patient, lab_admin, make_request, exam.id)
            ar.refresh_from_db()
            refs.add(ar.public_reference)
        assert len(refs) == 5

    def test_consecutive_creates_increment_sequence(
        self, patient, exam, lab_admin, make_request,
    ):
        RequestReferenceSequence.objects.all().delete()
        ar1 = _create(patient, lab_admin, make_request, exam.id)
        ar2 = _create(patient, lab_admin, make_request, exam.id)
        ar1.refresh_from_db()
        ar2.refresh_from_db()
        # Same day, so date prefix matches and sequence increments
        today = ar1.created_at.strftime('%Y%m%d')
        assert ar1.public_reference == f'{today}-000001'
        assert ar2.public_reference == f'{today}-000002'


# ---------------------------------------------------------------------------
# API exposure
# ---------------------------------------------------------------------------

class TestSerializerExposure:

    def test_detail_payload_includes_public_reference(
        self, patient, exam, lab_admin, make_request,
    ):
        ar = _create(patient, lab_admin, make_request, exam.id)
        client = APIClient(HTTP_HOST='testlab.localhost')
        client.force_authenticate(user=lab_admin)
        resp = client.get(f'{API}/{ar.id}/')
        body = _data(resp)
        assert body['request_number'] == ar.request_number
        assert body['public_reference'] == ar.public_reference

    def test_list_payload_includes_public_reference(
        self, patient, exam, lab_admin, make_request,
    ):
        _create(patient, lab_admin, make_request, exam.id)
        client = APIClient(HTTP_HOST='testlab.localhost')
        client.force_authenticate(user=lab_admin)
        resp = client.get(f'{API}/')
        body = _data(resp)
        # Envelope may wrap list results — unwrap common shapes
        items = body.get('results', body) if isinstance(body, dict) else body
        assert any('public_reference' in row and row['public_reference']
                   for row in items)


# ---------------------------------------------------------------------------
# Report integration
# ---------------------------------------------------------------------------

class TestReportIntegration:

    def _finalize(self, patient, exam, lab_admin, technician, biologist, make_request):
        from apps.results.services import ResultVersionService
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
                result_value='85',
                values=[{'value': '85', 'is_abnormal': False}],
                comments='',
            )
            ResultVersionService.submit(
                version=v, submitted_by=technician, request=req_tech,
            )
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

    def test_report_request_block_uses_public_reference(
        self, patient, exam,
        lab_admin, technician, biologist, make_request, monkeypatch,
    ):
        """
        The request reference is rendered via ``_draw_field_grid`` which
        calls ``canvas.drawString`` directly. We intercept at the canvas
        level to capture every string drawn on the report and verify the
        public_reference appears instead of the internal request_number.
        """
        from apps.requests import report_service
        from reportlab.pdfgen.canvas import Canvas

        drawn: list[str] = []
        orig_drawString = Canvas.drawString

        def spy_draw(self, x, y, text, *a, **kw):
            drawn.append(text)
            return orig_drawString(self, x, y, text, *a, **kw)

        monkeypatch.setattr(Canvas, 'drawString', spy_draw)

        ar = self._finalize(patient, exam, lab_admin, technician, biologist, make_request)
        report_service.RequestReportService.generate_or_get(
            analysis_request=ar, generated_by=biologist,
            request=make_request(biologist),
        )

        assert ar.public_reference in drawn
        assert ar.request_number not in drawn

    def test_download_filename_uses_public_reference(
        self, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = self._finalize(patient, exam, lab_admin, technician, biologist, make_request)
        client = APIClient(HTTP_HOST='testlab.localhost')
        client.force_authenticate(user=lab_admin)
        client.post(f'{API}/{ar.id}/report/')
        resp = client.get(f'{API}/{ar.id}/report/download/')
        assert resp.status_code == 200
        assert f'report_{ar.public_reference}' in resp['Content-Disposition']
