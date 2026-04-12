"""
Tests for request label generation.

Covers:
- Label count follows the N distinct families + fixed bonus rule
- Barcodes are system-wide unique and indexed
- Labels link back to the batch and the request (traceability)
- A PDF file is actually produced and stored
- The HTTP ``labels`` endpoint supports both GET (read) and POST (idempotent generate-or-get)
- Generating twice reuses the existing batch — no duplicate labels or barcodes
- Labels cannot be generated for a DRAFT request
- Permissions: non-admin read, non-reception write rejection
- Audit log is written once per generation
"""
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.storage import default_storage
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import ExamCategory, ExamDefinition, ExamFamily, SampleType
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest, ItemStatus, RequestLabel, RequestLabelBatch,
    RequestStatus, SourceType,
)
from apps.requests.label_service import (
    EXTRA_LABELS_BONUS, LabelCountStrategy, RequestLabelService,
)
from apps.requests.services import AnalysisRequestService


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
def tech_client(api_client, technician):
    api_client.force_authenticate(user=technician)
    return api_client


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-LBL-001',
        first_name='Alice',
        last_name='Labelled',
        date_of_birth=date(1990, 5, 20),
        gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family_a():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def family_b():
    return ExamFamily.objects.create(name='Biochemistry', display_order=2)


@pytest.fixture()
def family_c():
    return ExamFamily.objects.create(name='Immunology', display_order=3)


@pytest.fixture()
def exam_ham(family_a, category):
    return ExamDefinition.objects.create(
        category=category, family=family_a,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD, unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def exam_ham2(family_a, category):
    return ExamDefinition.objects.create(
        category=category, family=family_a,
        code='HBA1C', name='Glycated Hemoglobin',
        sample_type=SampleType.BLOOD, unit_price=Decimal('80.0000'),
    )


@pytest.fixture()
def exam_bio(family_b, category):
    return ExamDefinition.objects.create(
        category=category, family=family_b,
        code='GLU', name='Fasting Glucose',
        sample_type=SampleType.BLOOD, unit_price=Decimal('30.0000'),
    )


@pytest.fixture()
def exam_imm(family_c, category):
    return ExamDefinition.objects.create(
        category=category, family=family_c,
        code='CRP', name='C-Reactive Protein',
        sample_type=SampleType.BLOOD, unit_price=Decimal('40.0000'),
    )


def _create_confirmed_request(patient, lab_admin, make_request, exam_ids):
    """Create a request with inline items AND confirm it in one atomic call."""
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': eid} for eid in exam_ids],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )
    return ar


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# LabelCountStrategy — business rule isolation
# ---------------------------------------------------------------------------

class TestLabelCountStrategy:

    def test_single_family_yields_1_plus_bonus(
        self, patient, exam_ham, exam_ham2, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, make_request, [exam_ham.id, exam_ham2.id],
        )
        count, families = LabelCountStrategy.compute(ar)
        assert count == 1 + EXTRA_LABELS_BONUS
        assert families == ['Hematology']

    def test_three_families_yields_3_plus_bonus(
        self, patient, exam_ham, exam_bio, exam_imm, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, make_request,
            [exam_ham.id, exam_bio.id, exam_imm.id],
        )
        count, families = LabelCountStrategy.compute(ar)
        assert count == 3 + EXTRA_LABELS_BONUS
        assert set(families) == {'Hematology', 'Biochemistry', 'Immunology'}

    def test_distinct_families_dedupe_same_family_items(
        self, patient, exam_ham, exam_ham2, exam_bio, lab_admin, make_request,
    ):
        """Two items in Hematology + one in Biochemistry → 2 distinct families."""
        ar = _create_confirmed_request(
            patient, lab_admin, make_request,
            [exam_ham.id, exam_ham2.id, exam_bio.id],
        )
        count, families = LabelCountStrategy.compute(ar)
        assert count == 2 + EXTRA_LABELS_BONUS
        assert set(families) == {'Hematology', 'Biochemistry'}

    def test_rejected_items_excluded_from_count(
        self, patient, exam_ham, exam_bio, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, make_request, [exam_ham.id, exam_bio.id],
        )
        # Mark one item REJECTED directly (by-passing the state machine's
        # transition rules is OK here because we're testing the strategy,
        # not the workflow).
        item = ar.items.get(exam_definition=exam_bio)
        item.status = ItemStatus.REJECTED
        item.save(update_fields=['status', 'updated_at'])

        count, families = LabelCountStrategy.compute(ar)
        assert count == 1 + EXTRA_LABELS_BONUS
        assert families == ['Hematology']


# ---------------------------------------------------------------------------
# RequestLabelService.generate_or_get — core service behaviour
# ---------------------------------------------------------------------------

class TestGenerateOrGet:

    def test_generate_creates_batch_and_labels(
        self, patient, exam_ham, exam_bio, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, make_request, [exam_ham.id, exam_bio.id],
        )
        batch = RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )

        assert batch.analysis_request_id == ar.id
        assert batch.label_count == 2 + EXTRA_LABELS_BONUS
        assert batch.family_count == 2

        labels = list(batch.labels.order_by('label_index'))
        assert len(labels) == batch.label_count
        # First N labels pinned to families
        assert labels[0].family_name in {'Hematology', 'Biochemistry'}
        assert labels[1].family_name in {'Hematology', 'Biochemistry'}
        # Extras unpinned
        assert labels[2].family_name == ''
        assert labels[3].family_name == ''

    def test_barcodes_are_unique_within_batch(
        self, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, make_request, [exam_ham.id],
        )
        batch = RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        barcodes = [l.barcode_value for l in batch.labels.all()]
        assert len(barcodes) == len(set(barcodes))

    def test_barcodes_are_unique_system_wide(
        self, patient, exam_ham, exam_bio, lab_admin, make_request, category,
    ):
        """Generating labels for two different requests must yield
        disjoint barcode sets (system-wide uniqueness)."""
        ar1 = _create_confirmed_request(
            patient, lab_admin, make_request, [exam_ham.id],
        )
        p2 = Patient.objects.create(
            document_type='NATIONAL_ID_CARD',
            document_number='NID-LBL-002',
            first_name='Bob', last_name='Barcoded',
            date_of_birth=date(1975, 3, 10), gender='MALE',
            created_by=lab_admin,
        )
        ar2 = _create_confirmed_request(
            p2, lab_admin, make_request, [exam_bio.id],
        )
        b1 = RequestLabelService.generate_or_get(ar1, lab_admin, make_request(lab_admin))
        b2 = RequestLabelService.generate_or_get(ar2, lab_admin, make_request(lab_admin))

        set1 = {l.barcode_value for l in b1.labels.all()}
        set2 = {l.barcode_value for l in b2.labels.all()}
        assert set1.isdisjoint(set2)
        # And the DB-level unique constraint would reject a collision.
        assert RequestLabel.objects.count() == len(set1) + len(set2)

    def test_barcode_format_matches_convention(
        self, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        batch = RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        for label in batch.labels.all():
            assert label.barcode_value.startswith('LBL-')
            parts = label.barcode_value.split('-')
            assert len(parts) == 3
            assert len(parts[1]) == 8   # YYYYMMDD
            assert len(parts[2]) == 12  # 12 hex chars

    def test_pdf_is_generated_and_stored(
        self, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        batch = RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert batch.pdf_file_key
        assert default_storage.exists(batch.pdf_file_key)
        # Verify the file is non-empty and looks like a PDF
        with default_storage.open(batch.pdf_file_key, 'rb') as fh:
            header = fh.read(5)
        assert header == b'%PDF-'

    def test_idempotent_generate_returns_same_batch(
        self, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        first = RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        original_barcodes = {l.barcode_value for l in first.labels.all()}

        second = RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        second_barcodes = {l.barcode_value for l in second.labels.all()}

        assert second.id == first.id
        assert second_barcodes == original_barcodes
        # Still exactly one batch + N labels
        assert RequestLabelBatch.objects.filter(analysis_request=ar).count() == 1
        assert RequestLabel.objects.filter(batch=first).count() == first.label_count

    def test_cannot_generate_for_draft_request(
        self, patient, exam_ham, lab_admin, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_ham.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            # No confirm_after → stays DRAFT
        )
        assert ar.status == RequestStatus.DRAFT
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError):
            RequestLabelService.generate_or_get(
                analysis_request=ar,
                generated_by=lab_admin,
                request=make_request(lab_admin),
            )
        assert not RequestLabelBatch.objects.filter(analysis_request=ar).exists()


# ---------------------------------------------------------------------------
# Traceability — barcode → label → batch → request
# ---------------------------------------------------------------------------

class TestTraceability:

    def test_barcode_resolves_back_to_request(
        self, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        batch = RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        a_barcode = batch.labels.first().barcode_value

        label = RequestLabel.objects.select_related(
            'batch__analysis_request',
        ).get(barcode_value=a_barcode)
        assert label.batch_id == batch.id
        assert label.batch.analysis_request_id == ar.id
        assert label.batch.analysis_request.request_number == ar.request_number


# ---------------------------------------------------------------------------
# HTTP endpoint — ``GET`` and ``POST`` /requests/{id}/labels/
# ---------------------------------------------------------------------------

class TestLabelsEndpoint:

    def test_post_generates_and_returns_batch(
        self, admin_client, patient, exam_ham, exam_bio, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, make_request, [exam_ham.id, exam_bio.id],
        )
        resp = admin_client.post(f'{API}/{ar.id}/labels/')
        assert resp.status_code == 200, resp.content
        body = _data(resp)
        assert body['label_count'] == 2 + EXTRA_LABELS_BONUS
        assert body['family_count'] == 2
        # ``pdf_url`` is now the API-relative path of the protected
        # download endpoint — not a raw /media/ URL.
        assert body['pdf_url'] == f'/requests/{ar.id}/labels/download/'
        assert len(body['labels']) == body['label_count']
        # Labels sorted by label_index
        indices = [l['label_index'] for l in body['labels']]
        assert indices == sorted(indices)

    def test_get_returns_existing_batch(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        admin_client.post(f'{API}/{ar.id}/labels/')

        resp = admin_client.get(f'{API}/{ar.id}/labels/')
        assert resp.status_code == 200
        body = _data(resp)
        assert body['label_count'] == 1 + EXTRA_LABELS_BONUS

    def test_get_returns_404_when_no_batch_yet(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        resp = admin_client.get(f'{API}/{ar.id}/labels/')
        assert resp.status_code == 404

    def test_post_is_idempotent(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        first = admin_client.post(f'{API}/{ar.id}/labels/')
        assert first.status_code == 200
        first_id = _data(first)['id']

        second = admin_client.post(f'{API}/{ar.id}/labels/')
        assert second.status_code == 200
        assert _data(second)['id'] == first_id
        assert RequestLabelBatch.objects.filter(analysis_request=ar).count() == 1

    def test_post_rejected_for_draft_request(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_ham.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        resp = admin_client.post(f'{API}/{ar.id}/labels/')
        assert resp.status_code == 400

    def test_get_allowed_for_non_admin(
        self, tech_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        resp = tech_client.get(f'{API}/{ar.id}/labels/')
        assert resp.status_code == 200

    def test_post_forbidden_for_non_reception(
        self, tech_client, patient, exam_ham, lab_admin, make_request,
    ):
        """Technicians can read labels but not trigger generation."""
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        resp = tech_client.post(f'{API}/{ar.id}/labels/')
        assert resp.status_code == 403

    def test_generate_writes_audit_log(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        before = AuditLog.objects.filter(
            entity_type='RequestLabelBatch', action='CREATE',
        ).count()
        admin_client.post(f'{API}/{ar.id}/labels/')
        after = AuditLog.objects.filter(
            entity_type='RequestLabelBatch', action='CREATE',
        ).count()
        assert after == before + 1

    def test_idempotent_post_does_not_write_second_audit(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        admin_client.post(f'{API}/{ar.id}/labels/')
        before = AuditLog.objects.filter(
            entity_type='RequestLabelBatch', action='CREATE',
        ).count()
        admin_client.post(f'{API}/{ar.id}/labels/')
        after = AuditLog.objects.filter(
            entity_type='RequestLabelBatch', action='CREATE',
        ).count()
        assert after == before  # no second audit row


# ---------------------------------------------------------------------------
# Protected download endpoint
#
# Every byte of a generated label PDF must go through an authenticated,
# tenant-isolated backend endpoint. These tests lock in the core
# security rule: no raw media URL exposes the document.
# ---------------------------------------------------------------------------

class TestLabelsDownload:

    def test_authenticated_user_can_download_pdf(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        resp = admin_client.get(f'{API}/{ar.id}/labels/download/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'
        # FileResponse marks attachment downloads with a
        # ``Content-Disposition: attachment; filename=...`` header.
        disposition = resp['Content-Disposition']
        assert 'attachment' in disposition
        assert f'labels_{ar.request_number}.pdf' in disposition
        # The streamed bytes start with the canonical PDF magic header.
        body = b''.join(resp.streaming_content)
        assert body.startswith(b'%PDF-')

    def test_technician_can_download(
        self, tech_client, patient, exam_ham, lab_admin, make_request,
    ):
        """Any authenticated staff within the tenant can download
        labels — the read gate is ``IsAnyStaff``, matching the labels
        GET action."""
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        resp = tech_client.get(f'{API}/{ar.id}/labels/download/')
        assert resp.status_code == 200
        assert resp['Content-Type'] == 'application/pdf'

    def test_unauthenticated_user_cannot_download(
        self, api_client, patient, exam_ham, lab_admin, make_request,
    ):
        """No JWT → 401 Unauthorized. The sensitive document is never
        accessible without authentication, even to a caller who knows
        the request id."""
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        RequestLabelService.generate_or_get(
            analysis_request=ar,
            generated_by=lab_admin,
            request=make_request(lab_admin),
        )
        resp = api_client.get(f'{API}/{ar.id}/labels/download/')
        assert resp.status_code == 401

    def test_download_returns_404_when_no_batch(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        """Authenticated access still 404s when the target request
        has no generated batch. No information leak about whether the
        request exists vs does not have labels."""
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        resp = admin_client.get(f'{API}/{ar.id}/labels/download/')
        assert resp.status_code == 404

    def test_download_returns_404_for_unknown_request(self, admin_client):
        """An unknown request id returns 404 even to an authenticated
        caller. Tenant isolation is enforced by CytovaTenantMiddleware:
        a request id from another tenant is simply invisible in the
        current tenant's schema and resolves to a 404 here."""
        from uuid import uuid4
        resp = admin_client.get(f'{API}/{uuid4()}/labels/download/')
        assert resp.status_code == 404

    def test_generated_pdf_url_points_to_protected_endpoint(
        self, admin_client, patient, exam_ham, lab_admin, make_request,
    ):
        """The serialized ``pdf_url`` is the API-relative protected
        download path — never a raw /media/ URL. This guarantees that
        a client reading the label batch never sees a public media
        path at all."""
        ar = _create_confirmed_request(patient, lab_admin, make_request, [exam_ham.id])
        resp = admin_client.post(f'{API}/{ar.id}/labels/')
        body = _data(resp)
        pdf_url = body['pdf_url']
        assert pdf_url == f'/requests/{ar.id}/labels/download/'
        # Not a raw media URL — this is the critical security property.
        assert '/media/' not in pdf_url
        assert not pdf_url.startswith('http')
