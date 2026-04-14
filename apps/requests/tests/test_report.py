"""
Tests for the final patient report generation service.
"""
from datetime import date

import pytest
from django.core.files.storage import default_storage
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamParameter, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequestReport, RequestStatus, SourceType,
)
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


API = '/api/v1/requests'


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


@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def viewer_client(api_client, viewer_auditor):
    api_client.force_authenticate(user=viewer_auditor)
    return api_client


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-RPT-001',
        first_name='Rita',
        last_name='Reporter',
        date_of_birth=date(1988, 6, 6),
        gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def fam_hema():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def fam_bio():
    return ExamFamily.objects.create(name='Biochemistry', display_order=2)


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Spectrophotometry')


@pytest.fixture()
def exam_single(fam_bio, category, technique):
    return ExamDefinition.objects.create(
        category=category, family=fam_bio, technique=technique,
        code='GLU', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL',
        reference_range='70–100',
    )


@pytest.fixture()
def exam_multi(fam_hema, category, technique):
    exam = ExamDefinition.objects.create(
        category=category, family=fam_hema, technique=technique,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.MULTI_PARAMETER,
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='WBC', name='White Blood Cells',
        unit='10^3/uL', reference_range='4.5–11.0', display_order=1,
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='HGB', name='Hemoglobin',
        unit='g/dL', reference_range='12.0–16.0', display_order=2,
    )
    return exam


def _finalize_request(patient, lab_admin, technician, biologist, make_request, exams):
    """Create, collect, enter results, validate, finalize."""
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': e.id} for e in exams],
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
        exam = item.exam_definition
        if exam.result_structure == ResultStructure.MULTI_PARAMETER:
            params = list(exam.parameters.order_by('display_order'))
            values = [
                {'parameter_id': str(p.id), 'value': str(10 + i), 'is_abnormal': False}
                for i, p in enumerate(params)
            ]
            v = ResultVersionService.create_draft(
                item=item, entered_by=technician, request=req_tech,
                values=values,
                comments='Patient tolerated well.',
            )
        else:
            v = ResultVersionService.create_draft(
                item=item, entered_by=technician, request=req_tech,
                result_value='85',
                values=[{'value': '85', 'is_abnormal': False}],
                comments='Fasting confirmed.',
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


# ---------------------------------------------------------------------------
# Lifecycle & validation
# ---------------------------------------------------------------------------

class TestReportGeneration:

    def test_generates_pdf_for_finalized_request(
        self, patient, exam_single, exam_multi,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request,
            [exam_single, exam_multi],
        )
        assert ar.status == RequestStatus.VALIDATED

        report = RequestReportService.generate_or_get(
            analysis_request=ar,
            generated_by=biologist,
            request=make_request(biologist),
        )
        assert report.pdf_file_key
        assert default_storage.exists(report.pdf_file_key)

        with default_storage.open(report.pdf_file_key, 'rb') as f:
            content = f.read()
        assert content.startswith(b'%PDF')

    def test_non_finalized_request_blocked(
        self, patient, exam_single, lab_admin, biologist, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_single.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            confirm_after=True,
        )
        assert ar.status != RequestStatus.VALIDATED

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='validated requests'):
            RequestReportService.generate_or_get(
                analysis_request=ar,
                generated_by=biologist,
                request=make_request(biologist),
            )

    def test_idempotent_generate_or_get(
        self, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        r1 = RequestReportService.generate_or_get(
            analysis_request=ar, generated_by=biologist,
            request=make_request(biologist),
        )
        r2 = RequestReportService.generate_or_get(
            analysis_request=ar, generated_by=biologist,
            request=make_request(biologist),
        )
        assert r1.id == r2.id
        assert AnalysisRequestReport.objects.filter(analysis_request=ar).count() == 1

    def test_audit_written(
        self, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        AuditLog.objects.all().delete()
        RequestReportService.generate_or_get(
            analysis_request=ar, generated_by=biologist,
            request=make_request(biologist),
        )
        entry = AuditLog.objects.filter(
            entity_type='AnalysisRequestReport', action='CREATE',
        ).first()
        assert entry is not None
        assert entry.actor_email == biologist.email


# ---------------------------------------------------------------------------
# Data content
# ---------------------------------------------------------------------------

class TestReportContent:

    def test_groups_by_family(
        self, patient, exam_single, exam_multi,
        lab_admin, technician, biologist, make_request,
    ):
        from apps.requests.report_service import _collect_sections
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request,
            [exam_single, exam_multi],
        )
        sections = _collect_sections(ar)
        assert len(sections) == 2
        family_names = [s['family_name'] for s in sections]
        # Deterministic order: Hematology (display_order=1), Biochemistry (display_order=2)
        assert family_names == ['Hematology', 'Biochemistry']

    def test_single_value_section_data(
        self, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        from apps.requests.report_service import _collect_sections
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        sections = _collect_sections(ar)
        exam_data = sections[0]['exams'][0]
        assert exam_data['structure'] == ResultStructure.SINGLE_VALUE
        assert exam_data['code'] == 'GLU'
        assert exam_data['technique'] == 'Spectrophotometry'
        assert len(exam_data['values']) == 1
        assert exam_data['values'][0].value == '85'

    def test_multi_parameter_section_data(
        self, patient, exam_multi,
        lab_admin, technician, biologist, make_request,
    ):
        from apps.requests.report_service import _collect_sections
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_multi],
        )
        sections = _collect_sections(ar)
        exam_data = sections[0]['exams'][0]
        assert exam_data['structure'] == ResultStructure.MULTI_PARAMETER
        assert len(exam_data['values']) == 2
        codes = [v.name_snapshot for v in exam_data['values']]
        assert 'White Blood Cells' in codes
        assert 'Hemoglobin' in codes


# ---------------------------------------------------------------------------
# Display options
# ---------------------------------------------------------------------------

class TestDisplayOptions:

    def _render_direct(self, ar, settings):
        """Render directly to bytes using the internal renderer — bypasses
        storage + report row caching so tests can compare options cleanly."""
        import io
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from apps.requests.report_service import _render_report, _collect_sections

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        _render_report(c, ar, settings, _collect_sections(ar))
        c.save()
        return len(buf.getvalue())

    def test_technique_option_toggles_pdf_content(
        self, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )

        settings = LabSettings.get_solo()
        settings.show_exam_technique = True
        size_on = self._render_direct(ar, settings)

        settings.show_exam_technique = False
        size_off = self._render_direct(ar, settings)

        assert size_on != size_off

    def test_final_conclusion_toggles_pdf_content(
        self, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        settings = LabSettings.get_solo()
        settings.show_final_conclusion = True

        size_empty = self._render_direct(ar, settings)

        ar.final_conclusion = 'Patient values are within normal ranges.'
        size_with = self._render_direct(ar, settings)

        assert size_with > size_empty

    def test_comments_option_toggles_pdf_content(
        self, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )

        settings = LabSettings.get_solo()
        settings.show_patient_comments = True
        size_on = self._render_direct(ar, settings)

        settings.show_patient_comments = False
        size_off = self._render_direct(ar, settings)

        assert size_on != size_off


# ---------------------------------------------------------------------------
# Endpoints & security
# ---------------------------------------------------------------------------

class TestReportEndpoints:

    def test_generate_endpoint(
        self, admin_client, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        resp = admin_client.post(f'{API}/{ar.id}/report/')
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert body['pdf_url'] == f'/requests/{ar.id}/report/download/'

    def test_generate_rejected_for_non_finalized(
        self, admin_client, patient, exam_single,
        lab_admin, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_single.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            confirm_after=True,
        )
        resp = admin_client.post(f'{API}/{ar.id}/report/')
        assert resp.status_code == 400

    def test_download_streams_pdf(
        self, admin_client, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        admin_client.post(f'{API}/{ar.id}/report/')
        resp = admin_client.get(f'{API}/{ar.id}/report/download/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'
        assert b'attachment' in resp['Content-Disposition'].encode()

    def test_download_404_when_no_report(
        self, admin_client, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        resp = admin_client.get(f'{API}/{ar.id}/report/download/')
        assert resp.status_code == 404

    def test_unauthenticated_blocked(self, api_client, patient, exam_single,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(
            patient, lab_admin, technician, biologist, make_request, [exam_single],
        )
        resp = api_client.get(f'{API}/{ar.id}/report/download/')
        assert resp.status_code == 401
