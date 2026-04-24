"""
Tests for brute-force protection on password verification.

Covers:
- Failed attempts increment
- Correct password resets attempts
- After 5 failures token is locked (429)
- Locked token rejects verification
- After lock expiry verification is allowed again
- Download still works after successful verification
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.patient_access import ResultAccessService
from apps.requests.report_service import RequestReportService
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


@pytest.fixture(autouse=True)
def _enable_protection():
    lab = LabSettings.get_solo()
    lab.result_pdf_password_enabled = True
    lab.result_pdf_password_mode = 'PATIENT_DOB'
    lab.save(update_fields=[
        'result_pdf_password_enabled', 'result_pdf_password_mode', 'updated_at',
    ])


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-BF-001',
        first_name='Brute', last_name='Force',
        date_of_birth=date(1990, 6, 15), gender='MALE',
        phone='12345678',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='L', display_order=1)
    fam = ExamFamily.objects.create(name='B', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='BF', name='BFTest',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('10'),
    )


@pytest.fixture()
def token(patient, exam, lab_admin, technician, biologist, make_request):
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
    return ResultAccessService.create_token(ar)


def _verify(token_str, password):
    c = APIClient(HTTP_HOST='testlab.localhost')
    return c.post(
        f'/r/{token_str}/verify-password/',
        {'password': password},
        format='json',
    )


class TestBruteForceProtection:

    CORRECT = '19900615'  # PATIENT_DOB mode
    WRONG = 'wrongpassword'

    def test_failed_attempt_increments(self, token):
        resp = _verify(token.token, self.WRONG)
        assert resp.status_code == 403
        token.refresh_from_db()
        assert token.failed_attempts == 1

    def test_correct_password_resets_attempts(self, token):
        _verify(token.token, self.WRONG)
        _verify(token.token, self.WRONG)
        token.refresh_from_db()
        assert token.failed_attempts == 2

        resp = _verify(token.token, self.CORRECT)
        assert resp.status_code == 200
        token.refresh_from_db()
        assert token.failed_attempts == 0
        assert token.verified_at is not None

    def test_lockout_after_5_failures(self, token):
        for _ in range(5):
            _verify(token.token, self.WRONG)
        token.refresh_from_db()
        assert token.failed_attempts == 5
        assert token.locked_until is not None
        assert token.is_locked

    def test_locked_token_returns_429(self, token):
        for _ in range(5):
            _verify(token.token, self.WRONG)
        resp = _verify(token.token, self.CORRECT)
        assert resp.status_code == 429
        body = resp.json()
        assert 'retry_after' in body

    def test_lock_expires_allows_retry(self, token):
        for _ in range(5):
            _verify(token.token, self.WRONG)
        # Manually expire the lock
        token.refresh_from_db()
        token.locked_until = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=['locked_until'])

        resp = _verify(token.token, self.CORRECT)
        assert resp.status_code == 200
        token.refresh_from_db()
        assert token.failed_attempts == 0

    def test_remaining_attempts_in_response(self, token):
        resp = _verify(token.token, self.WRONG)
        body = resp.json()
        assert body['remaining_attempts'] == 4

    def test_download_works_after_verification(self, token):
        resp = _verify(token.token, self.CORRECT)
        assert resp.status_code == 200
        body = resp.json()
        grant = (body.get('data') or body).get('download_grant')
        assert grant

        c = APIClient(HTTP_HOST='testlab.localhost')
        dl = c.get(f'/r/{token.token}/download/?grant={grant}')
        assert dl.status_code == 200
        assert dl['Content-Type'] == 'application/pdf'
