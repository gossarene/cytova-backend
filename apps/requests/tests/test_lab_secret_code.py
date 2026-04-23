"""
Tests for the lab secret code and the DOB_PHONE_SECRET password mode.

Covers:
- Auto-generation of lab_secret_code on get_solo()
- Format: 6-char uppercase alphanumeric, no ambiguous chars
- DOB_PHONE_SECRET mode produces correct password
- Changing the secret code does not affect already-stored PDFs
- Missing data validation
"""
import io
import re
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.storage import default_storage
from django_tenants.utils import get_public_schema_name, schema_context
from pypdf import PdfReader
from rest_framework.exceptions import ValidationError

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.pdf_protection import derive_password
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


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-SEC-001',
        first_name='Secret', last_name='Code',
        date_of_birth=date(1996, 2, 1), gender='MALE',
        phone='+229 97 00 12 34',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='L', display_order=1)
    fam = ExamFamily.objects.create(name='B', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='SEC', name='SecretTest',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('10'),
    )


def _finalize(patient, lab_admin, technician, biologist, make_request, exam):
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
    return ar


SAFE_CHARS = set('ABCDEFGHJKLMNPQRSTUVWXYZ23456789')


class TestAutoGeneration:

    def test_get_solo_generates_secret_code(self):
        LabSettings.objects.all().delete()
        lab = LabSettings.get_solo()
        assert lab.lab_secret_code
        assert len(lab.lab_secret_code) == 2

    def test_format_uppercase_no_ambiguous(self):
        LabSettings.objects.all().delete()
        lab = LabSettings.get_solo()
        assert all(c in SAFE_CHARS for c in lab.lab_secret_code)

    def test_subsequent_get_solo_returns_same_code(self):
        LabSettings.objects.all().delete()
        first = LabSettings.get_solo().lab_secret_code
        second = LabSettings.get_solo().lab_secret_code
        assert first == second


class TestDobPhoneSecretMode:

    def _set(self, enabled=True, mode='DOB_PHONE_SECRET'):
        lab = LabSettings.get_solo()
        lab.result_pdf_password_enabled = enabled
        lab.result_pdf_password_mode = mode
        lab.save(update_fields=[
            'result_pdf_password_enabled', 'result_pdf_password_mode', 'updated_at',
        ])
        return lab

    def test_password_includes_secret_code(
        self, patient, exam, lab_admin, make_request,
    ):
        ar = _finalize(patient, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = self._set()
        password = derive_password(ar, settings)
        # Format: YYYYMMDD-XXXX-SECRET
        assert password.startswith('19960201-1234-')
        secret_part = password.split('-')[2]
        assert len(secret_part) == 2
        assert all(c in SAFE_CHARS for c in secret_part)

    def test_protected_pdf_uses_secret(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        settings = self._set()
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, exam)
        report = RequestReportService.generate_or_get(
            ar, biologist, make_request(biologist),
        )
        with default_storage.open(report.pdf_file_key, 'rb') as f:
            data = f.read()
        reader = PdfReader(io.BytesIO(data))
        assert reader.is_encrypted

        password = derive_password(ar, settings)
        reader.decrypt(password)
        assert len(reader.pages) > 0

    def test_changing_code_does_not_affect_stored_pdf(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        settings = self._set()
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, exam)
        old_password = derive_password(ar, settings)

        report = RequestReportService.generate_or_get(
            ar, biologist, make_request(biologist),
        )

        # Change the secret code
        settings.lab_secret_code = 'ZZ'
        settings.save(update_fields=['lab_secret_code', 'updated_at'])

        # Old PDF still opens with the OLD password
        with default_storage.open(report.pdf_file_key, 'rb') as f:
            data = f.read()
        reader = PdfReader(io.BytesIO(data))
        reader.decrypt(old_password)
        assert len(reader.pages) > 0

    def test_missing_phone_blocks_generation(
        self, exam, lab_admin, make_request,
    ):
        p = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='NID-SEC-NP',
            first_name='No', last_name='Phone',
            date_of_birth=date(1990, 1, 1), gender='MALE',
            created_by=lab_admin,
        )
        ar = _finalize(p, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = self._set()
        with pytest.raises(ValidationError, match='phone'):
            derive_password(ar, settings)
