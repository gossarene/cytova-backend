"""
Tests for result PDF password protection.

Covers:
- Each password mode derives the correct password
- Missing data blocks generation with clear error
- Protected PDF requires password to read
- Unprotected mode passes through unchanged
- No regression in report generation
"""
import io
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
from apps.requests.pdf_protection import derive_password, encrypt_pdf, protect_if_enabled
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
        document_type='NATIONAL_ID_CARD', document_number='NID-PWD-001',
        first_name='Secure', last_name='Patient',
        date_of_birth=date(1996, 2, 1), gender='MALE',
        phone='+229 97 00 12 34',
        created_by=lab_admin,
    )


@pytest.fixture()
def patient_no_phone(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-PWD-002',
        first_name='No', last_name='Phone',
        date_of_birth=date(1990, 6, 15), gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='L', display_order=1)
    fam = ExamFamily.objects.create(name='B', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='PWD', name='PwdTest',
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


def _set_protection(enabled, mode='DOB_PLUS_PHONE_SUFFIX'):
    lab = LabSettings.get_solo()
    lab.result_pdf_password_enabled = enabled
    lab.result_pdf_password_mode = mode
    lab.save(update_fields=[
        'result_pdf_password_enabled', 'result_pdf_password_mode', 'updated_at',
    ])
    return lab


# ---------------------------------------------------------------------------
# Password derivation
# ---------------------------------------------------------------------------

class TestPasswordDerivation:

    def test_dob_mode(self, patient, lab_admin, make_request, exam):
        ar = _finalize(patient, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = _set_protection(True, 'PATIENT_DOB')
        assert derive_password(ar, settings) == '19960201'

    def test_phone_mode(self, patient, lab_admin, make_request, exam):
        ar = _finalize(patient, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = _set_protection(True, 'PATIENT_PHONE')
        assert derive_password(ar, settings) == '22997001234'

    def test_request_reference_mode(self, patient, lab_admin, make_request, exam):
        ar = _finalize(patient, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = _set_protection(True, 'REQUEST_REFERENCE')
        assert derive_password(ar, settings) == ar.public_reference

    def test_dob_plus_phone_suffix(self, patient, lab_admin, make_request, exam):
        ar = _finalize(patient, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = _set_protection(True, 'DOB_PLUS_PHONE_SUFFIX')
        assert derive_password(ar, settings) == '19960201-1234'


# ---------------------------------------------------------------------------
# Validation — missing data blocks generation
# ---------------------------------------------------------------------------

class TestMissingDataValidation:

    def test_phone_mode_missing_phone(self, patient_no_phone, lab_admin, make_request, exam):
        ar = _finalize(patient_no_phone, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = _set_protection(True, 'PATIENT_PHONE')
        with pytest.raises(ValidationError, match='phone'):
            derive_password(ar, settings)

    def test_phone_suffix_mode_missing_phone(self, patient_no_phone, lab_admin, make_request, exam):
        ar = _finalize(patient_no_phone, lab_admin, lab_admin, lab_admin, make_request, exam)
        settings = _set_protection(True, 'DOB_PLUS_PHONE_SUFFIX')
        with pytest.raises(ValidationError, match='phone'):
            derive_password(ar, settings)


# ---------------------------------------------------------------------------
# PDF encryption — real file-level protection
# ---------------------------------------------------------------------------

class TestPdfEncryption:

    def test_encrypted_pdf_requires_password(self):
        # Generate a minimal PDF
        from reportlab.pdfgen import canvas as rc
        from reportlab.lib.pagesizes import A4
        buf = io.BytesIO()
        c = rc.Canvas(buf, pagesize=A4)
        c.drawString(100, 700, 'Test content')
        c.save()
        raw = buf.getvalue()

        encrypted = encrypt_pdf(raw, 'secret123')

        # Attempting to read without password should fail or show encrypted
        reader = PdfReader(io.BytesIO(encrypted))
        assert reader.is_encrypted

        # With correct password, content is accessible
        reader.decrypt('secret123')
        assert len(reader.pages) > 0

    def test_protect_if_disabled_passes_through(self):
        raw = b'%PDF-1.4 test'
        settings = _set_protection(False)
        result = protect_if_enabled(raw, None, settings)
        assert result == raw


# ---------------------------------------------------------------------------
# Integration — report generation with protection
# ---------------------------------------------------------------------------

class TestReportIntegration:

    def test_protected_report_stored(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        _set_protection(True, 'DOB_PLUS_PHONE_SUFFIX')
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, exam)
        report = RequestReportService.generate_or_get(
            ar, biologist, make_request(biologist),
        )
        assert report.pdf_file_key
        with default_storage.open(report.pdf_file_key, 'rb') as f:
            data = f.read()
        reader = PdfReader(io.BytesIO(data))
        assert reader.is_encrypted

    def test_unprotected_report_when_disabled(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        _set_protection(False)
        ar = _finalize(patient, lab_admin, technician, biologist, make_request, exam)
        report = RequestReportService.generate_or_get(
            ar, biologist, make_request(biologist),
        )
        with default_storage.open(report.pdf_file_key, 'rb') as f:
            data = f.read()
        reader = PdfReader(io.BytesIO(data))
        assert not reader.is_encrypted

    def test_missing_data_blocks_report_generation(
        self, exam, lab_admin, technician, biologist, make_request,
    ):
        _set_protection(True, 'DOB_PLUS_PHONE_SUFFIX')
        p = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='NID-PWD-BLOCK',
            first_name='Block', last_name='Test', gender='MALE',
            date_of_birth=date(1990, 1, 1),
            created_by=lab_admin,
        )
        ar = _finalize(p, lab_admin, technician, biologist, make_request, exam)
        with pytest.raises(ValidationError, match='phone'):
            RequestReportService.generate_or_get(
                ar, biologist, make_request(biologist),
            )
