"""Tests for laboratory logo upload/delete/preview + PDF rendering."""
import io

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.lab_settings.models import LabSettings


API = '/api/v1/lab-settings/'


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
def admin_client(lab_admin):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=lab_admin)
    return c


@pytest.fixture()
def viewer_client(viewer_auditor):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=viewer_auditor)
    return c


def _png_bytes() -> bytes:
    """Minimal valid PNG (1x1 transparent pixel)."""
    import base64
    return base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
    )


class TestLogoUpload:

    def test_admin_can_upload_logo(self, admin_client):
        f = SimpleUploadedFile('lab.png', _png_bytes(), content_type='image/png')
        resp = admin_client.post(f'{API}logo/', {'file': f}, format='multipart')
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert body['has_logo_file'] is True
        assert body['logo_preview_url'] == '/lab-settings/logo/'

        settings = LabSettings.get_solo()
        assert settings.logo_file_key.startswith('lab-settings/logo/')
        assert default_storage.exists(settings.logo_file_key)

    def test_viewer_cannot_upload_logo(self, viewer_client):
        f = SimpleUploadedFile('lab.png', _png_bytes(), content_type='image/png')
        resp = viewer_client.post(f'{API}logo/', {'file': f}, format='multipart')
        assert resp.status_code == 403

    def test_rejects_non_image(self, admin_client):
        f = SimpleUploadedFile('doc.pdf', b'%PDF-fake', content_type='application/pdf')
        resp = admin_client.post(f'{API}logo/', {'file': f}, format='multipart')
        assert resp.status_code == 400

    def test_delete_logo(self, admin_client):
        f = SimpleUploadedFile('lab.png', _png_bytes(), content_type='image/png')
        admin_client.post(f'{API}logo/', {'file': f}, format='multipart')

        resp = admin_client.delete(f'{API}logo/')
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert body['has_logo_file'] is False
        assert body['logo_preview_url'] is None

    def test_replace_logo_cleans_old_file(self, admin_client):
        f1 = SimpleUploadedFile('a.png', _png_bytes(), content_type='image/png')
        admin_client.post(f'{API}logo/', {'file': f1}, format='multipart')
        old_key = LabSettings.get_solo().logo_file_key

        f2 = SimpleUploadedFile('b.png', _png_bytes(), content_type='image/png')
        admin_client.post(f'{API}logo/', {'file': f2}, format='multipart')
        new_key = LabSettings.get_solo().logo_file_key

        assert old_key != new_key
        assert not default_storage.exists(old_key)
        assert default_storage.exists(new_key)


class TestLogoDownload:

    def test_any_staff_can_preview_logo(self, admin_client, viewer_client):
        f = SimpleUploadedFile('lab.png', _png_bytes(), content_type='image/png')
        admin_client.post(f'{API}logo/', {'file': f}, format='multipart')

        resp = viewer_client.get(f'{API}logo/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'image/png'

    def test_404_when_no_logo(self, admin_client):
        resp = admin_client.get(f'{API}logo/')
        assert resp.status_code == 404

    def test_unauthenticated_blocked(self, api_client):
        resp = api_client.get(f'{API}logo/')
        assert resp.status_code == 401


class TestLogoUrlField:

    def test_logo_url_is_writable_via_patch(self, admin_client):
        resp = admin_client.patch(
            API,
            {'logo_url': 'https://acme.example.com/logo.png'},
            format='json',
        )
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert body['logo_url'] == 'https://acme.example.com/logo.png'
        assert body['has_logo_file'] is False  # upload is the primary path


class TestReportUsesUploadedLogo:

    def test_report_pdf_size_grows_with_logo(
        self, admin_client, lab_admin, technician, biologist, make_request,
    ):
        """PDF rendering with a logo should produce a different/larger output
        than rendering without one, proving the logo is actually embedded."""
        # Minimal setup: a validated request
        from datetime import date
        from apps.catalog.models import (
            ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
            ResultStructure, SampleType,
        )
        from apps.patients.models import Patient
        from apps.requests.services import (
            AnalysisRequestItemService, AnalysisRequestService,
        )
        from apps.results.services import ResultVersionService
        from apps.requests.models import SourceType
        from apps.requests.report_service import (
            _collect_sections, _render_report, _RenderContext,
        )
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4

        patient = Patient.objects.create(
            document_type='NATIONAL_ID_CARD',
            document_number='NID-LOGO-001',
            first_name='Logo', last_name='Test',
            date_of_birth=date(1990, 1, 1),
            gender='FEMALE',
            created_by=lab_admin,
        )
        ExamCategory.objects.create(name='Labs', display_order=1)
        fam = ExamFamily.objects.create(name='Hematology', display_order=1)
        tech = ExamTechnique.objects.create(name='Test')
        exam = ExamDefinition.objects.create(
            family=fam, technique=tech,
            code='HGB', name='Hemoglobin',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit='g/dL',
        )
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
        item = ar.items.first()
        req_t = make_request(technician)
        req_b = make_request(biologist)
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req_t,
        )
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_t,
            result_value='14.5', values=[{'value': '14.5'}],
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
        v.refresh_from_db()
        ResultVersionService.validate(
            version=v, validation_notes='', validated_by=biologist, request=req_b,
        )
        ar.refresh_from_db()
        AnalysisRequestService.finalize_validation(
            analysis_request=ar, finalized_by=biologist, request=req_b,
        )
        ar.refresh_from_db()

        settings = LabSettings.get_solo()
        settings.show_logo = True
        settings.save()

        # 1) Render without logo
        buf1 = io.BytesIO()
        c1 = canvas.Canvas(buf1, pagesize=A4)
        _render_report(c1, ar, settings, _collect_sections(ar), _RenderContext())
        c1.save()
        size_without = len(buf1.getvalue())

        # 2) Upload a logo and render again
        f = SimpleUploadedFile('lab.png', _png_bytes(), content_type='image/png')
        admin_client.post(f'{API}logo/', {'file': f}, format='multipart')
        settings = LabSettings.get_solo()

        buf2 = io.BytesIO()
        c2 = canvas.Canvas(buf2, pagesize=A4)
        _render_report(c2, ar, settings, _collect_sections(ar), _RenderContext())
        c2.save()
        size_with = len(buf2.getvalue())

        assert size_with > size_without
