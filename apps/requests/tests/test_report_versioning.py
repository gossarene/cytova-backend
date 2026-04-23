"""
Tests for versioned final reports (generate_or_get + regenerate).

Covers:
- First generate creates version 1 with is_current=True
- Revisiting the request still exposes current report availability
- Regenerate creates version 2 and keeps version 1
- The "current" pointer switches correctly on regenerate
- Download streams the current version
- Non-finalized requests cannot generate or regenerate
- Unauthorized access is blocked
- Detail serializer exposes has_report / current_report
"""
from datetime import date

import pytest
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.patients.models import Patient
from apps.requests.models import AnalysisRequestReport, RequestStatus, SourceType
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


API = '/api/v1/requests'


# ---------------------------------------------------------------------------
# Subscription fixture (same pattern as sibling test modules)
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
# Domain fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-RPV-001',
        first_name='Rita', last_name='Reporter',
        date_of_birth=date(1988, 6, 6), gender='FEMALE',
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
        code='GLU-V', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70–100',
    )


def _finalize_request(patient, lab_admin, technician, biologist, make_request, exam):
    """Create → collect → enter → validate → finalize (matches test_report.py)."""
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


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Service-level versioning behaviour
# ---------------------------------------------------------------------------

class TestVersioningService:

    def test_first_generate_creates_version_1(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        report = RequestReportService.generate_or_get(
            analysis_request=ar, generated_by=biologist,
            request=make_request(biologist),
        )
        assert report.version_number == 1
        assert report.is_current is True

    def test_generate_is_idempotent(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        r1 = RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
        r2 = RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
        assert r1.id == r2.id
        assert AnalysisRequestReport.objects.filter(analysis_request=ar).count() == 1

    def test_regenerate_creates_v2_and_switches_current(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        v1 = RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
        v2 = RequestReportService.regenerate(ar, biologist, make_request(biologist))

        assert v2.id != v1.id
        assert v2.version_number == 2
        assert v2.is_current is True

        v1.refresh_from_db()
        assert v1.is_current is False
        assert v1.pdf_file_key  # historical file remains

        # Still only ONE current version per request
        current_count = AnalysisRequestReport.objects.filter(
            analysis_request=ar, is_current=True,
        ).count()
        assert current_count == 1

    def test_regenerate_monotonic_version_numbers(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
        RequestReportService.regenerate(ar, biologist, make_request(biologist))
        v3 = RequestReportService.regenerate(ar, biologist, make_request(biologist))
        assert v3.version_number == 3
        total = AnalysisRequestReport.objects.filter(analysis_request=ar).count()
        assert total == 3

    def test_regenerate_requires_existing_version(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        with pytest.raises(ValidationError):
            RequestReportService.regenerate(ar, biologist, make_request(biologist))

    def test_regenerate_blocked_for_non_finalized(
        self, patient, exam, lab_admin, biologist, make_request,
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
        with pytest.raises(ValidationError, match='validated requests'):
            RequestReportService.regenerate(ar, biologist, make_request(biologist))

    def test_get_current_returns_latest_current(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
        v2 = RequestReportService.regenerate(ar, biologist, make_request(biologist))
        current = RequestReportService.get_current(ar)
        assert current.id == v2.id
        assert current.version_number == 2


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

class TestHttpEndpoints:

    def test_get_report_returns_current_after_generate(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        resp = admin_client.get(f'{API}/{ar.id}/report/')
        assert resp.status_code == 200
        body = _data(resp)
        assert body['version_number'] == 1
        assert body['is_current'] is True

    def test_get_report_404_before_any_generation(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        resp = admin_client.get(f'{API}/{ar.id}/report/')
        assert resp.status_code == 404

    def test_regenerate_endpoint_creates_v2(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        resp = admin_client.post(f'{API}/{ar.id}/report/regenerate/')
        assert resp.status_code == 200
        body = _data(resp)
        assert body['version_number'] == 2
        assert body['is_current'] is True

    def test_regenerate_without_prior_is_400(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        resp = admin_client.post(f'{API}/{ar.id}/report/regenerate/')
        assert resp.status_code == 400

    def test_download_streams_current_version(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        admin_client.post(f'{API}/{ar.id}/report/regenerate/')

        resp = admin_client.get(f'{API}/{ar.id}/report/download/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'
        assert 'v2' in resp['Content-Disposition']

    def test_download_unauthenticated_blocked(
        self, patient, exam,
        admin_client, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        # Fresh, unauthenticated client — admin_client shares its instance
        # with the project's api_client fixture, so requesting a new one
        # here avoids inheriting the admin JWT.
        fresh = APIClient(HTTP_HOST='testlab.localhost')
        resp = fresh.get(f'{API}/{ar.id}/report/download/')
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Detail serializer exposure — stable across page reload
# ---------------------------------------------------------------------------

class TestDetailPayloadExposure:

    def test_has_report_false_before_generation(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        resp = admin_client.get(f'{API}/{ar.id}/')
        body = _data(resp)
        assert body['has_report'] is False
        assert body['current_report'] is None

    def test_current_report_exposed_after_generate(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')

        # Simulate the user leaving and returning — just re-fetch detail
        resp = admin_client.get(f'{API}/{ar.id}/')
        body = _data(resp)
        assert body['has_report'] is True
        assert body['current_report']['version_number'] == 1
        assert body['current_report']['downloadable'] is True
        assert body['current_report']['pdf_url'] == f'/requests/{ar.id}/report/download/'

    def test_current_report_updates_after_regenerate(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        admin_client.post(f'{API}/{ar.id}/report/regenerate/')

        resp = admin_client.get(f'{API}/{ar.id}/')
        body = _data(resp)
        assert body['current_report']['version_number'] == 2


# ---------------------------------------------------------------------------
# Report history — list + version-specific secure download
# ---------------------------------------------------------------------------

class TestReportHistory:

    def test_versions_list_is_empty_before_any_generation(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        resp = admin_client.get(f'{API}/{ar.id}/report/versions/')
        assert resp.status_code == 200
        assert _data(resp)['results'] == []

    def test_versions_list_returns_all_versions_newest_first(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        admin_client.post(f'{API}/{ar.id}/report/regenerate/')
        admin_client.post(f'{API}/{ar.id}/report/regenerate/')

        resp = admin_client.get(f'{API}/{ar.id}/report/versions/')
        assert resp.status_code == 200
        versions = _data(resp)['results']
        assert [v['version_number'] for v in versions] == [3, 2, 1]

        currents = [v for v in versions if v['is_current']]
        assert len(currents) == 1
        assert currents[0]['version_number'] == 3

        for v in versions:
            assert v['pdf_url'] == (
                f'/requests/{ar.id}/report/versions/{v["id"]}/download/'
            )
            assert v['downloadable'] is True
            assert '/media/' not in v['pdf_url']

    def test_download_specific_historical_version(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        admin_client.post(f'{API}/{ar.id}/report/regenerate/')

        resp = admin_client.get(f'{API}/{ar.id}/report/versions/')
        v1 = next(v for v in _data(resp)['results'] if v['version_number'] == 1)

        dl = admin_client.get(
            f'{API}/{ar.id}/report/versions/{v1["id"]}/download/',
        )
        assert dl.status_code == 200
        assert dl['Content-Type'] == 'application/pdf'
        assert 'v1' in dl['Content-Disposition']
        body = b''.join(dl.streaming_content)
        assert body.startswith(b'%PDF-')

    def test_current_download_endpoint_still_works(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        admin_client.post(f'{API}/{ar.id}/report/regenerate/')

        resp = admin_client.get(f'{API}/{ar.id}/report/download/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'
        # Current endpoint always follows the pointer — must be v2
        assert 'v2' in resp['Content-Disposition']

    def test_version_from_another_request_is_404(
        self, admin_client, patient, exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar_a = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        ar_b = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar_a.id}/report/')
        admin_client.post(f'{API}/{ar_b.id}/report/')

        resp = admin_client.get(f'{API}/{ar_b.id}/report/versions/')
        b_report_id = _data(resp)['results'][0]['id']

        # Using ar_A's path with ar_B's report id must 404 — the version
        # does not belong to that request.
        cross = admin_client.get(
            f'{API}/{ar_a.id}/report/versions/{b_report_id}/download/',
        )
        assert cross.status_code == 404

    def test_versions_list_unauthenticated_blocked(
        self, patient, exam,
        admin_client, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        fresh = APIClient(HTTP_HOST='testlab.localhost')
        resp = fresh.get(f'{API}/{ar.id}/report/versions/')
        assert resp.status_code == 401

    def test_version_download_unauthenticated_blocked(
        self, patient, exam,
        admin_client, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_request(patient, lab_admin, technician, biologist, make_request, exam)
        admin_client.post(f'{API}/{ar.id}/report/')
        r = admin_client.get(f'{API}/{ar.id}/report/versions/')
        report_id = _data(r)['results'][0]['id']

        fresh = APIClient(HTTP_HOST='testlab.localhost')
        resp = fresh.get(
            f'{API}/{ar.id}/report/versions/{report_id}/download/',
        )
        assert resp.status_code == 401
