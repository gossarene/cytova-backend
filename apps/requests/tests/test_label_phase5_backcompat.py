"""
Phase 5 — Final integration sweep for the flexible-labels rollout.

The four prior phases each pinned a slice of the contract. Phase 5
ties them together with the load-bearing claim of the entire
rollout: **a tenant on default settings produces label batches
that are bit-for-bit identical to what the pre-Phase-1 code would
have produced**. Without this guarantee, every existing tenant's
barcode behaviour would silently drift on the next deploy.

Four scenarios pin the integration:

1. **Back-compat acceptance** — touch nothing in lab settings,
   generate a 3-family batch, assert the resulting shape matches
   the pre-Phase-1 baseline exactly (5 labels, all distinct,
   ``TTTTYYMMSSSSSS`` format, family-name assignment, monthly
   ``LabelSequence`` row).
2. **SAME_REQUEST_NUMBER end-to-end** — through the HTTP endpoint,
   not just the service. Proves the new mode flows through every
   layer (view → service → allocator → renderer → storage).
3. **Sequence-advance accounting** — pin that a SAME_REQUEST_NUMBER
   batch advances ``LabelSequence.last_value`` by exactly ONE
   regardless of N labels. The whole point of the mode is that
   it doesn't burn N budget per batch.
4. **Mode-switch invariance** — flipping settings after a batch is
   generated never mutates the batch. The OneToOne lifecycle rule
   plus snapshot-at-write semantics guarantee this; pinning it
   here means a future refactor can't accidentally re-render
   historical batches under fresh settings.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, SampleType,
)
from apps.lab_settings.models import (
    LabelNumberingMode, LabelSequenceResetPeriod, LabSettings,
)
from apps.patients.models import Patient
from apps.requests.label_service import RequestLabelService
from apps.requests.models import (
    LabelSequence, RequestLabel, RequestStatus, SourceType,
)
from apps.requests.services import AnalysisRequestService


API = '/api/v1/requests'

# Disable the conftest's autouse "auto-generate labels on confirmation"
# wrapper so each test owns when generation runs.
pytestmark = pytest.mark.no_auto_labels


# ---------------------------------------------------------------------------
# Subscription fixture (mirrors sibling label suites)
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
        document_number=f'NID-P5-{_DOC_SEQ:04d}',
        first_name='Bob', last_name='Backcompat',
        date_of_birth=date(1985, 3, 14), gender='MALE',
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
def exam_a(category, family_a, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family_a, technique=default_technique,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD, unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def exam_b(category, family_b, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family_b, technique=default_technique,
        code='GLU', name='Fasting Glucose',
        sample_type=SampleType.BLOOD, unit_price=Decimal('30.0000'),
    )


@pytest.fixture()
def exam_c(category, family_c, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family_c, technique=default_technique,
        code='CRP', name='C-Reactive Protein',
        sample_type=SampleType.BLOOD, unit_price=Decimal('40.0000'),
    )


@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


def _confirmed(patient, lab_admin, make_request, exam_ids):
    return AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': eid} for eid in exam_ids],
        },
        created_by=lab_admin, request=make_request(lab_admin),
        confirm_after=True,
    )


# ---------------------------------------------------------------------------
# 1. Back-compat acceptance — defaults reproduce pre-Phase-1 behaviour
# ---------------------------------------------------------------------------

class TestPrePhase1BehaviourPreserved:

    def test_default_tenant_three_families_yields_pre_rollout_baseline(
        self, patient, exam_a, exam_b, exam_c, lab_admin, make_request,
    ):
        """The load-bearing acceptance claim of the rollout: a
        tenant that touches NOTHING in lab settings continues to
        produce the same five-label batches the pre-Phase-1 code
        produced. If this drifts, every existing tenant gets
        unexpectedly different barcodes on the next deploy.

        Pinned here without setting ANY field on LabSettings —
        relies entirely on the migration defaults to reproduce the
        baseline. Phase 1's TestLabelGenerationDefaults proves the
        defaults match; this test proves they keep matching when
        the full pipeline runs."""
        # No _set_settings() call — the defaults from Phase 1's
        # migration carry the load.
        ar = _confirmed(patient, lab_admin, make_request,
                        [exam_a.id, exam_b.id, exam_c.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )

        # Five labels (3 families + 2 default extras).
        assert batch.label_count == 5
        assert batch.family_count == 3

        labels = list(batch.labels.order_by('label_index'))
        assert len(labels) == 5

        # First three carry family names (in family display_order).
        family_names = [l.family_name for l in labels]
        assert family_names[0] == 'Hematology'
        assert family_names[1] == 'Biochemistry'
        assert family_names[2] == 'Immunology'
        # Trailing two are extras.
        assert family_names[3] == ''
        assert family_names[4] == ''

        # Five distinct barcodes (PER_FAMILY default).
        barcodes = [l.barcode_value for l in labels]
        assert len(set(barcodes)) == 5

        # Format unchanged: TTTTYYMMSSSSSS (14 digits).
        for code in barcodes:
            assert code.isdigit()
            assert len(code) == 14

        # Sequence row created under the monthly key (YYYY-MM).
        from django.utils import timezone
        today = timezone.now().date()
        expected_period = f'{today.year:04d}-{today.month:02d}'
        seq = LabelSequence.objects.get(period_key=expected_period)
        # Five allocations advanced last_value by 5.
        assert seq.last_value == 5

        # PDF rendered + stored — proves the renderer wasn't broken
        # by the model/service refactors.
        from django.core.files.storage import default_storage
        assert batch.pdf_file_key
        assert default_storage.exists(batch.pdf_file_key)


# ---------------------------------------------------------------------------
# 2. SAME_REQUEST_NUMBER end-to-end through the HTTP endpoint
# ---------------------------------------------------------------------------

class TestSameRequestNumberE2E:

    def test_http_post_labels_with_same_request_mode(
        self, admin_client, patient, exam_a, exam_b, exam_c,
        lab_admin, make_request,
    ):
        """Configure SAME_REQUEST_NUMBER + 2 extras, hit the public
        labels endpoint, confirm the batch comes back with all
        five labels sharing one barcode AND the PDF was rendered.
        Proves the new mode works through every layer end-to-end."""
        s = LabSettings.get_solo()
        s.label_numbering_mode = LabelNumberingMode.SAME_REQUEST_NUMBER
        s.extra_label_count = 2
        s.save()

        ar = _confirmed(patient, lab_admin, make_request,
                        [exam_a.id, exam_b.id, exam_c.id])
        resp = admin_client.post(f'{API}/{ar.id}/labels/')
        assert resp.status_code in (200, 201), resp.content

        # Read back via the database — the model is the canonical
        # source for what was actually persisted.
        labels = list(RequestLabel.objects.filter(
            batch__analysis_request=ar,
        ).order_by('label_index'))
        assert len(labels) == 5
        codes = {l.barcode_value for l in labels}
        assert len(codes) == 1, (
            f'SAME_REQUEST_NUMBER end-to-end must emit ONE shared '
            f'barcode across 5 labels; got {len(codes)}: {codes}'
        )
        # PDF rendered + stored even with the unusual mode.
        from django.core.files.storage import default_storage
        assert ar.label_batch.pdf_file_key
        assert default_storage.exists(ar.label_batch.pdf_file_key)


# ---------------------------------------------------------------------------
# 3. Sequence-advance accounting — SAME_REQUEST_NUMBER calls allocator once
# ---------------------------------------------------------------------------

class TestSequenceAdvanceAccounting:

    def test_per_family_advances_sequence_by_label_count(
        self, patient, exam_a, exam_b, exam_c, lab_admin, make_request,
    ):
        """Sanity baseline: PER_FAMILY mode advances the sequence
        by N (one allocation per label). 3 families + 2 extras
        ⇒ 5 increments."""
        s = LabSettings.get_solo()
        s.label_numbering_mode = LabelNumberingMode.PER_FAMILY
        s.extra_label_count = 2
        s.save()
        LabelSequence.objects.all().delete()

        ar = _confirmed(patient, lab_admin, make_request,
                        [exam_a.id, exam_b.id, exam_c.id])
        RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        seq = LabelSequence.objects.get()
        assert seq.last_value == 5

    def test_same_request_number_advances_sequence_by_exactly_one(
        self, patient, exam_a, exam_b, exam_c, lab_admin, make_request,
    ):
        """The load-bearing optimization. Without this, SAME_REQUEST_NUMBER
        would burn the same per-period budget as PER_FAMILY, which
        would defeat the entire point of the mode."""
        s = LabSettings.get_solo()
        s.label_numbering_mode = LabelNumberingMode.SAME_REQUEST_NUMBER
        s.extra_label_count = 2  # 5 labels total
        s.save()
        LabelSequence.objects.all().delete()

        ar = _confirmed(patient, lab_admin, make_request,
                        [exam_a.id, exam_b.id, exam_c.id])
        RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        seq = LabelSequence.objects.get()
        # FIVE labels, ONE allocation. Sequence advanced by 1.
        assert seq.last_value == 1

    def test_back_to_back_same_request_number_batches_advance_one_each(
        self, patient, exam_a, exam_b, lab_admin, make_request,
    ):
        """Two consecutive SAME_REQUEST_NUMBER batches each consume
        ONE sequence value. Proves the per-batch accounting holds
        across the OneToOne batch lifecycle and that the second
        batch starts at last_value+1, not +N."""
        s = LabSettings.get_solo()
        s.label_numbering_mode = LabelNumberingMode.SAME_REQUEST_NUMBER
        s.extra_label_count = 1
        s.save()
        LabelSequence.objects.all().delete()

        ar1 = _confirmed(patient, lab_admin, make_request, [exam_a.id])
        ar2 = _confirmed(patient, lab_admin, make_request, [exam_b.id])

        b1 = RequestLabelService.generate_or_get(
            ar1, lab_admin, make_request(lab_admin),
        )
        b2 = RequestLabelService.generate_or_get(
            ar2, lab_admin, make_request(lab_admin),
        )
        # Trailing 6 digits of each batch's barcode reflect the
        # sequence value at allocation time. b1 got value 1; b2
        # got value 2.
        b1_seq = int(b1.labels.first().barcode_value[-6:])
        b2_seq = int(b2.labels.first().barcode_value[-6:])
        assert (b1_seq, b2_seq) == (1, 2)
        # And the row's last_value reflects two total advances.
        seq = LabelSequence.objects.get()
        assert seq.last_value == 2


# ---------------------------------------------------------------------------
# 4. Mode-switch invariance — old batches stay frozen
# ---------------------------------------------------------------------------

class TestModeSwitchInvariance:

    def test_flipping_every_setting_does_not_mutate_existing_batch(
        self, patient, exam_a, exam_b, exam_c, lab_admin, make_request,
    ):
        """Generate a batch under defaults, then flip every label-
        related setting at once: numbering_mode, extras, reset
        period. The historical batch's labels must be byte-for-byte
        unchanged. Pin'd because a future refactor that
        accidentally re-renders on settings change would silently
        rewrite traceability records."""
        # Step 1 — generate under defaults.
        ar = _confirmed(patient, lab_admin, make_request,
                        [exam_a.id, exam_b.id, exam_c.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        # Snapshot the batch state.
        before = sorted(
            (l.label_index, l.family_name, l.barcode_value)
            for l in batch.labels.all()
        )
        before_pdf_key = batch.pdf_file_key
        before_label_count = batch.label_count

        # Step 2 — flip every relevant setting.
        s = LabSettings.get_solo()
        s.label_numbering_mode = LabelNumberingMode.SAME_REQUEST_NUMBER
        s.extra_label_count = 0
        s.label_sequence_reset_period = LabelSequenceResetPeriod.YEARLY
        s.save()

        # Step 3 — re-fetch the historical batch.
        batch.refresh_from_db()
        after = sorted(
            (l.label_index, l.family_name, l.barcode_value)
            for l in batch.labels.all()
        )

        # Every label row identical: same index, same family_name,
        # same barcode_value.
        assert after == before
        # Batch metadata unchanged too.
        assert batch.label_count == before_label_count
        assert batch.pdf_file_key == before_pdf_key

    def test_idempotent_generate_returns_original_batch_after_settings_change(
        self, patient, exam_a, lab_admin, make_request,
    ):
        """The OneToOne lifecycle ("generate once and reuse") is
        the load-bearing rule that protects against accidental
        re-render under new settings. A second ``generate_or_get``
        call after a settings flip must return the EXISTING batch
        unchanged, NOT regenerate under the new config."""
        # Generate under defaults.
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id])
        first = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        first_id = first.id
        first_codes = {l.barcode_value for l in first.labels.all()}

        # Flip settings to SAME_REQUEST_NUMBER + extras=5. If
        # generate_or_get were to re-run, the second batch would
        # have 6 labels (1 family + 5 extras) all sharing one
        # barcode — visibly different from the original.
        s = LabSettings.get_solo()
        s.label_numbering_mode = LabelNumberingMode.SAME_REQUEST_NUMBER
        s.extra_label_count = 5
        s.save()

        # Second call — same OneToOne row, no re-allocation.
        second = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        assert second.id == first_id
        assert {l.barcode_value for l in second.labels.all()} == first_codes
        assert second.label_count == first.label_count
