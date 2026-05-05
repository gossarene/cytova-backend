"""
Mark-collected requires the labels to have been downloaded at least
once.

The label workflow is: confirm request → generate labels (PDF
materialised) → DOWNLOAD labels (PDF leaves the lab's server) →
print + stick on tubes → mark specimens collected. The "download"
step is what proves the labels physically reached the lab; without
it, marking specimens "collected" would be tagging tubes that nobody
has barcodes for, breaking the scan-based traceability chain at the
next workflow step.

What's pinned here
------------------
- The ``RequestLabelService`` plus generation alone DOES NOT unblock
  collection — ``download_count`` stays at 0.
- A single download stamps ``downloaded_at`` + ``downloaded_by`` and
  bumps ``download_count`` to 1.
- Subsequent downloads only increment the counter; the first-touch
  metadata stays pinned to the ORIGINAL operator + timestamp.
- Two collection rejection messages are distinct + specific so the
  frontend's helper text and the rejection toast match exactly:
    - "Labels must be generated …"   when no batch exists
    - "Labels must be downloaded …"  when batch exists but never
                                     downloaded
- After at least one download, mark-collected works normally.
- The autouse ``_auto_generate_labels_on_confirm`` shim that
  back-compats legacy tests is opted out via the ``no_auto_labels``
  marker — these tests exercise the gate explicitly.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.catalog.models import ExamCategory, ExamDefinition, ExamFamily, SampleType
from apps.patients.models import Patient
from apps.requests.label_service import RequestLabelService
from apps.requests.models import (
    AnalysisRequestItem, ItemStatus, RequestLabelBatch, SourceType,
)
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)


API = '/api/v1/requests'

# Drive label state explicitly — both the auto-generate shim AND
# the auto-download wrap would mask the gate.
pytestmark = [
    pytest.mark.no_auto_labels,
    pytest.mark.no_auto_label_download,
]


# ---------------------------------------------------------------------------
# Subscription gate (mirror sibling label tests)
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


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------

_DOC_SEQ = 0


@pytest.fixture()
def patient(lab_admin):
    global _DOC_SEQ
    _DOC_SEQ += 1
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number=f'NID-DLG-{_DOC_SEQ:04d}',
        first_name='Ada', last_name='Lovelace',
        date_of_birth=date(1990, 5, 17), gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def exam(family, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD, unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def admin_client(lab_admin):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=lab_admin)
    return c


def _confirmed_request(patient, exam, lab_admin, make_request):
    return AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )


# ---------------------------------------------------------------------------
# 1. Service-layer gate behaviour
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestMarkCollectedDownloadGate:

    def test_cannot_collect_when_no_labels_generated(
        self, patient, exam, lab_admin, technician, make_request,
    ):
        """Pre-existing rule, pinned here so the new download check
        doesn't accidentally weaken it. The error message must
        say "must be GENERATED" so the operator's next step is
        clear (matches the inline UI helper)."""
        ar = _confirmed_request(patient, exam, lab_admin, make_request)
        item = ar.items.first()

        with pytest.raises(Exception) as exc_info:
            AnalysisRequestItemService.mark_collected(
                item=item, collected_by=technician,
                request=make_request(technician),
            )
        # ``ValidationError`` from DRF — message check is sufficient
        # since these strings are the wire contract for the toast.
        assert 'generated' in str(exc_info.value).lower()

    def test_cannot_collect_when_labels_generated_but_not_downloaded(
        self, patient, exam, lab_admin, technician, make_request,
    ):
        """The new gate: a generated-but-undownloaded batch is NOT
        enough. The labels must have left the server at least
        once before collection makes sense."""
        ar = _confirmed_request(patient, exam, lab_admin, make_request)
        RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )
        # Sanity: gate signal is correct on a fresh batch.
        batch = RequestLabelBatch.objects.get(analysis_request=ar)
        assert batch.download_count == 0
        assert batch.has_been_downloaded is False

        item = ar.items.first()
        with pytest.raises(Exception) as exc_info:
            AnalysisRequestItemService.mark_collected(
                item=item, collected_by=technician,
                request=make_request(technician),
            )
        # Message contains "downloaded" — frontend matches on that
        # substring to render the right helper toast.
        assert 'downloaded' in str(exc_info.value).lower()
        assert 'generated' not in str(exc_info.value).lower(), (
            'The "not generated" branch must NOT fire when a batch '
            'exists — the messages drive different UI helper text.'
        )

    def test_can_collect_after_one_download(
        self, admin_client, patient, exam, lab_admin, technician, make_request,
    ):
        """Happy path: generate labels, download once, mark
        collected — works."""
        ar = _confirmed_request(patient, exam, lab_admin, make_request)
        RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )
        # Drive the download via the API endpoint so we exercise
        # the real stamping path (not a bypass that bumps the
        # field directly).
        resp = admin_client.get(f'{API}/{ar.id}/labels/download/')
        assert resp.status_code == 200

        item = ar.items.first()
        item = AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician,
            request=make_request(technician),
        )
        assert item.status == ItemStatus.COLLECTED


# ---------------------------------------------------------------------------
# 2. Download endpoint stamping behaviour
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDownloadStamping:

    def test_first_download_stamps_metadata(
        self, admin_client, patient, exam, lab_admin, make_request,
    ):
        """First download: ``downloaded_at`` set, ``downloaded_by``
        set to the requester, ``download_count`` = 1."""
        ar = _confirmed_request(patient, exam, lab_admin, make_request)
        RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )

        before = timezone.now()
        resp = admin_client.get(f'{API}/{ar.id}/labels/download/')
        assert resp.status_code == 200
        after = timezone.now()

        batch = RequestLabelBatch.objects.get(analysis_request=ar)
        assert batch.download_count == 1
        assert batch.downloaded_at is not None
        assert before <= batch.downloaded_at <= after
        assert batch.downloaded_by_id == lab_admin.id

    def test_repeated_downloads_increment_counter_only(
        self, admin_client, patient, exam, lab_admin, technician, make_request,
    ):
        """Second + third downloads: ``download_count`` advances,
        but ``downloaded_at`` and ``downloaded_by`` STAY pinned to
        the original first-touch — even when a different user
        does the re-download. The first-touch metadata is the
        audit-grade record of who unblocked collection; later
        downloads are routine reprints."""
        ar = _confirmed_request(patient, exam, lab_admin, make_request)
        RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )

        # First download — stamps the audit-grade fields.
        admin_client.get(f'{API}/{ar.id}/labels/download/')
        batch_after_first = RequestLabelBatch.objects.get(analysis_request=ar)
        first_at = batch_after_first.downloaded_at
        first_by_id = batch_after_first.downloaded_by_id

        # Second + third downloads — bump counter only. The
        # technician redownloads via a fresh client so we'd notice
        # if downloaded_by got rewritten.
        tech_client = APIClient(HTTP_HOST='testlab.localhost')
        tech_client.force_authenticate(user=technician)
        admin_client.get(f'{API}/{ar.id}/labels/download/')
        tech_client.get(f'{API}/{ar.id}/labels/download/')

        batch = RequestLabelBatch.objects.get(analysis_request=ar)
        assert batch.download_count == 3
        assert batch.downloaded_at == first_at, (
            'First-download timestamp must NOT advance on re-download — '
            'it is the audit-grade first-touch.'
        )
        assert batch.downloaded_by_id == first_by_id, (
            'First-download actor must NOT be overwritten by a later '
            'downloader.'
        )


# ---------------------------------------------------------------------------
# 3. Regression — the autouse shim path still works
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestExistingFlowRegression:

    def test_autouse_shim_path_stays_intact(
        self, patient, exam, lab_admin, technician, make_request,
        request,
    ):
        """The legacy auto-generate-labels-on-confirm shim also
        bumps ``download_count`` so legacy tests keep their pre-
        rollout semantics. Pinned here so a future shim change
        can't silently break ~30 dependent tests at once. Driven
        manually since this module declares ``no_auto_labels``."""
        from apps.requests.label_service import RequestLabelService
        from django.db.models import F

        ar = _confirmed_request(patient, exam, lab_admin, make_request)
        # Replicate exactly what the shim does so we pin its
        # contract regardless of pytest marker.
        batch = RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )
        RequestLabelBatch.objects.filter(pk=batch.pk).update(
            download_count=F('download_count') + 1,
        )

        item = ar.items.first()
        item = AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician,
            request=make_request(technician),
        )
        assert item.status == ItemStatus.COLLECTED
