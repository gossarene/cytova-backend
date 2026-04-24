"""
Tests for secure patient result access tokens.

Covers:
- Token creation from finalized request with report
- Valid token grants access metadata
- Valid token grants PDF download
- Expired token blocked
- Revoked token blocked
- Invalid/unknown token blocked
- No report → token creation blocked
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.storage import default_storage
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.patients.models import Patient
from apps.requests.models import ResultAccessToken, SourceType
from apps.requests.patient_access import ResultAccessService
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


pytestmark = pytest.mark.no_auto_labels
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


@pytest.fixture(autouse=True)
def _disable_pdf_protection():
    lab = LabSettings.get_solo()
    lab.result_pdf_password_enabled = False
    lab.save(update_fields=['result_pdf_password_enabled', 'updated_at'])


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-ACC-001',
        first_name='Access', last_name='Token',
        date_of_birth=date(1990, 1, 1), gender='MALE',
        phone='12345678',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='L', display_order=1)
    fam = ExamFamily.objects.create(name='B', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='ACC', name='AccessTest',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('10'),
    )


def _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam):
    from apps.requests.label_service import RequestLabelService
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
    RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
    return ar


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------

class TestTokenCreation:

    def test_creates_valid_token(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        token = ResultAccessService.create_token(ar)
        assert len(token.token) == 64
        assert token.is_active
        assert token.expires_at > timezone.now()
        assert token.report_file_key

    def test_no_report_raises(
        self, patient, exam, lab_admin, make_request,
    ):
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
        with pytest.raises(ValidationError, match='no report PDF'):
            ResultAccessService.create_token(ar)


class TestTokenValidation:

    def test_valid_token(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        token = ResultAccessService.create_token(ar)
        validated = ResultAccessService.validate_token(token.token)
        assert validated.id == token.id

    def test_expired_token(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        token = ResultAccessService.create_token(ar, ttl_hours=0)
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=['expires_at'])
        with pytest.raises(ValidationError, match='expired'):
            ResultAccessService.validate_token(token.token)

    def test_revoked_token(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        token = ResultAccessService.create_token(ar)
        ResultAccessService.revoke_token(token)
        with pytest.raises(ValidationError, match='revoked'):
            ResultAccessService.validate_token(token.token)

    def test_unknown_token(self):
        with pytest.raises(ValidationError, match='Invalid'):
            ResultAccessService.validate_token('nonexistent_token_value')


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------

class TestAccessEndpoints:

    @pytest.fixture()
    def token(self, patient, exam, lab_admin, technician, biologist, make_request):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        return ResultAccessService.create_token(ar)

    def test_access_returns_metadata_without_identity(self, token):
        """Metadata endpoint must NOT expose patient name before verification."""
        c = APIClient(HTTP_HOST='testlab.localhost')
        resp = c.get(f'/r/{token.token}/')
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert 'patient_name' not in body
        assert 'request_reference' not in body
        assert body['downloadable'] is True
        assert 'password_required' in body

    def test_download_streams_pdf(self, token):
        c = APIClient(HTTP_HOST='testlab.localhost')
        resp = c.get(f'/r/{token.token}/download/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'
        body = b''.join(resp.streaming_content)
        assert body[:5] == b'%PDF-'

    def test_expired_token_returns_403(self, token):
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=['expires_at'])
        c = APIClient(HTTP_HOST='testlab.localhost')
        resp = c.get(f'/r/{token.token}/')
        assert resp.status_code == 403

    def test_invalid_token_returns_403(self):
        c = APIClient(HTTP_HOST='testlab.localhost')
        resp = c.get('/r/totally_fake_token/')
        assert resp.status_code == 403

    def test_create_token_endpoint(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        c = APIClient(HTTP_HOST='testlab.localhost')
        c.force_authenticate(user=lab_admin)
        resp = c.post(f'{API}/{ar.id}/access-token/')
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert 'token' in body
        assert '/results/access/' in body['access_url']
        assert body['access_url'].endswith(body['token'])
        assert body['download_url'].endswith('/download/')
