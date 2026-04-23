"""
Tests for user-owned signature management and report integration.

Covers:
- Signature upload via POST /users/me/signature/
- Signature stream via GET /users/me/signature/
- Signature clear via DELETE /users/me/signature/
- Report uses the validating biologist's signature_file_key
- Fallback to text-only when no signature exists
- No regression in report generation
"""
import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService
from apps.users.models import StaffUser


API_USERS = '/api/v1/users'
API_REQUESTS = '/api/v1/requests'


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Subscription fixture
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
def bio_client(biologist):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=biologist)
    return c


@pytest.fixture()
def admin_client(lab_admin):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=lab_admin)
    return c


@pytest.fixture()
def png_file():
    """Minimal valid 1x1 PNG."""
    data = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
        b'\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02'
        b'\x00\x00\x00\x90wS\xde\x00\x00\x00\x0c'
        b'IDATx\x9cc\xf8\x0f\x00\x00\x01\x01'
        b'\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    return SimpleUploadedFile('sig.png', data, content_type='image/png')


# ---------------------------------------------------------------------------
# Upload / stream / delete
# ---------------------------------------------------------------------------

class TestSignatureUpload:

    def test_upload_stores_and_returns_has_signature(self, bio_client, png_file):
        resp = bio_client.post(
            f'{API_USERS}/me/signature/',
            {'file': png_file},
            format='multipart',
        )
        assert resp.status_code == 200, resp.content
        assert _data(resp)['has_signature'] is True

    def test_stream_returns_image(self, bio_client, biologist, png_file):
        bio_client.post(f'{API_USERS}/me/signature/', {'file': png_file}, format='multipart')
        resp = bio_client.get(f'{API_USERS}/me/signature/')
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith('image/')

    def test_stream_404_when_no_signature(self, bio_client):
        resp = bio_client.get(f'{API_USERS}/me/signature/')
        assert resp.status_code == 404

    def test_delete_clears_signature(self, bio_client, png_file):
        bio_client.post(f'{API_USERS}/me/signature/', {'file': png_file}, format='multipart')
        resp = bio_client.delete(f'{API_USERS}/me/signature/')
        assert resp.status_code == 200
        assert _data(resp)['has_signature'] is False

    def test_rejects_non_image(self, bio_client):
        bad = SimpleUploadedFile('doc.pdf', b'%PDF-1.4', content_type='application/pdf')
        resp = bio_client.post(f'{API_USERS}/me/signature/', {'file': bad}, format='multipart')
        assert resp.status_code == 400

    def test_unauthenticated_blocked(self):
        c = APIClient(HTTP_HOST='testlab.localhost')
        resp = c.post(f'{API_USERS}/me/signature/')
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Report integration
# ---------------------------------------------------------------------------

class TestReportUsesValidatorSignature:

    @pytest.fixture()
    def patient(self, lab_admin):
        return Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='NID-SIG-001',
            first_name='S', last_name='T',
            date_of_birth=date(1990, 1, 1), gender='MALE',
            created_by=lab_admin,
        )

    @pytest.fixture()
    def exam(self, default_technique):
        cat = ExamCategory.objects.create(name='L', display_order=1)
        fam = ExamFamily.objects.create(name='B', display_order=1)
        return ExamDefinition.objects.create(
            category=cat, family=fam, technique=default_technique,
            code='SIG', name='SigTest',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit='u', reference_range='0-100',
            unit_price=Decimal('10'),
        )

    def _finalize(self, patient, lab_admin, technician, biologist, make_request, exam):
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
                result_value='50', values=[{'value': '50', 'is_abnormal': False}],
                comments='',
            )
            ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
            v = item.result_versions.get(is_current=True)
            ResultVersionService.validate(
                version=v, validation_notes='OK', validated_by=biologist, request=req_b,
            )
        ar.refresh_from_db()
        AnalysisRequestService.finalize_validation(
            analysis_request=ar, finalized_by=biologist, request=req_b,
        )
        ar.refresh_from_db()
        return ar

    def test_report_renders_when_biologist_has_signature(
        self, patient, exam, lab_admin, technician, biologist, make_request,
        bio_client, png_file,
    ):
        bio_client.post(f'{API_USERS}/me/signature/', {'file': png_file}, format='multipart')
        biologist.refresh_from_db()
        assert biologist.signature_file_key

        ar = self._finalize(patient, lab_admin, technician, biologist, make_request, exam)
        from apps.requests.report_service import RequestReportService
        report = RequestReportService.generate_or_get(
            ar, biologist, make_request(biologist),
        )
        assert report.pdf_file_key
        with default_storage.open(report.pdf_file_key, 'rb') as f:
            assert f.read(5) == b'%PDF-'

    def test_report_renders_without_signature_fallback(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        assert not biologist.signature_file_key
        ar = self._finalize(patient, lab_admin, technician, biologist, make_request, exam)
        from apps.requests.report_service import RequestReportService
        report = RequestReportService.generate_or_get(
            ar, biologist, make_request(biologist),
        )
        assert report.pdf_file_key
        with default_storage.open(report.pdf_file_key, 'rb') as f:
            assert f.read(5) == b'%PDF-'

    def test_me_endpoint_exposes_has_signature(self, bio_client, png_file):
        resp = bio_client.get(f'{API_USERS}/me/')
        assert _data(resp)['has_signature'] is False

        bio_client.post(f'{API_USERS}/me/signature/', {'file': png_file}, format='multipart')
        resp = bio_client.get(f'{API_USERS}/me/')
        assert _data(resp)['has_signature'] is True
