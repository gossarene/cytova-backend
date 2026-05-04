"""
Phase 4 — Label numbering modes + extra_label_count + barcode unique drop.

Three new behaviours land here:

1. ``LabSettings.extra_label_count`` replaces the hard-coded
   ``EXTRA_LABELS_BONUS = 2``. The constant survives as the
   documented default (back-compat for tests + new tenants).
2. ``LabSettings.label_numbering_mode`` selects between PER_FAMILY
   (one fresh sequence value per label, the historical behaviour)
   and SAME_REQUEST_NUMBER (one allocation per BATCH, reused on
   every label row in the batch).
3. The DB-level ``unique=True`` on ``RequestLabel.barcode_value``
   was dropped — required for SAME_REQUEST_NUMBER to even insert.
   Cross-batch uniqueness is preserved by the locked
   ``LabelSequence`` allocator (Phase 3 invariant).

Each test sets ``LabSettings.get_solo()`` explicitly so the assertion
isn't coupled to whatever the default happens to be the day the
test runs.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditAction, AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, SampleType,
)
from apps.lab_settings.models import (
    LabelNumberingMode, LabSettings,
)
from apps.patients.models import Patient
from apps.requests.label_service import (
    LabelCountStrategy, RequestLabelService,
)
from apps.requests.models import (
    RequestLabel, RequestStatus, SourceType,
)
from apps.requests.services import AnalysisRequestService


# Disable the conftest's autouse "auto-generate labels on confirmation"
# wrapper so each test owns when generation runs.
pytestmark = pytest.mark.no_auto_labels


# ---------------------------------------------------------------------------
# Subscription fixture — sibling label tests need this to confirm requests
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
        document_number=f'NID-NM-{_DOC_SEQ:04d}',
        first_name='Alice', last_name='Numbered',
        date_of_birth=date(1990, 5, 20), gender='FEMALE',
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


def _exam(category, family, default_technique, *, code, name):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code=code, name=name,
        sample_type=SampleType.BLOOD, unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def exam_a(category, family_a, default_technique):
    return _exam(category, family_a, default_technique,
                 code='CBC', name='Complete Blood Count')


@pytest.fixture()
def exam_b(category, family_b, default_technique):
    return _exam(category, family_b, default_technique,
                 code='GLU', name='Fasting Glucose')


@pytest.fixture()
def exam_c(category, family_c, default_technique):
    return _exam(category, family_c, default_technique,
                 code='CRP', name='C-Reactive Protein')


@pytest.fixture()
def exam_no_family(category, default_technique):
    """Exam without a family — used for the "extras only" edge
    case. The label strategy currently skips items without a
    family entirely (it only counts distinct families), so a
    request built solely from this exam has zero family-labels."""
    return ExamDefinition.objects.create(
        category=category, family=None, technique=default_technique,
        code='UNF', name='Unfamilied Assay',
        sample_type=SampleType.BLOOD, unit_price=Decimal('20.0000'),
    )


def _confirmed(patient, lab_admin, make_request, exam_ids):
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': eid} for eid in exam_ids],
        },
        created_by=lab_admin, request=make_request(lab_admin),
        confirm_after=True,
    )
    return ar


def _set_settings(*, mode=None, extras=None, reset=None):
    """Tiny helper — pin one or more LabSettings in a single call so
    the per-test setup stays one expressive line."""
    s = LabSettings.get_solo()
    if mode is not None:
        s.label_numbering_mode = mode
    if extras is not None:
        s.extra_label_count = extras
    if reset is not None:
        s.label_sequence_reset_period = reset
    s.save()


# ---------------------------------------------------------------------------
# 1. PER_FAMILY mode — historical behaviour, preserved
# ---------------------------------------------------------------------------

class TestPerFamilyMode:

    def test_three_families_plus_two_extras_yields_five_distinct_codes(
        self, patient, exam_a, exam_b, exam_c, lab_admin, make_request,
    ):
        """Default config: 3 families + 2 extras = 5 labels with 5
        distinct barcodes. The pre-Phase-4 baseline; if this drifts,
        every existing tenant's batches change shape on the next
        deploy."""
        _set_settings(mode=LabelNumberingMode.PER_FAMILY, extras=2)
        ar = _confirmed(patient, lab_admin, make_request,
                        [exam_a.id, exam_b.id, exam_c.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )

        codes = [lbl.barcode_value for lbl in batch.labels.order_by('label_index')]
        assert batch.label_count == 5
        assert len(codes) == 5
        # Every barcode unique within the batch — the load-bearing
        # invariant for PER_FAMILY mode.
        assert len(set(codes)) == 5

    def test_family_names_assigned_to_first_n_labels(
        self, patient, exam_a, exam_b, lab_admin, make_request,
    ):
        """Family-label assignment rule: indices [1..family_count]
        carry the family name; trailing extras carry the empty
        string. The PDF renderer relies on this to decide whether
        to print the family caption on a tube."""
        _set_settings(mode=LabelNumberingMode.PER_FAMILY, extras=2)
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id, exam_b.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )

        ordered = list(batch.labels.order_by('label_index'))
        family_names = [lbl.family_name for lbl in ordered]
        # First two labels carry family names; last two are extras.
        assert family_names[0] in {'Hematology', 'Biochemistry'}
        assert family_names[1] in {'Hematology', 'Biochemistry'}
        assert family_names[0] != family_names[1]
        assert family_names[2] == ''
        assert family_names[3] == ''


# ---------------------------------------------------------------------------
# 2. SAME_REQUEST_NUMBER mode — single shared barcode
# ---------------------------------------------------------------------------

class TestSameRequestNumberMode:

    def test_all_labels_share_one_barcode_value(
        self, patient, exam_a, exam_b, exam_c, lab_admin, make_request,
    ):
        """SAME_REQUEST_NUMBER's defining behaviour: every label
        in the batch carries the same barcode. This used to be
        impossible — the old DB-level unique constraint refused
        the second insert. Phase 4 dropped that constraint."""
        _set_settings(mode=LabelNumberingMode.SAME_REQUEST_NUMBER, extras=2)
        ar = _confirmed(patient, lab_admin, make_request,
                        [exam_a.id, exam_b.id, exam_c.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )

        codes = {lbl.barcode_value for lbl in batch.labels.all()}
        assert batch.label_count == 5
        assert len(codes) == 1, (
            f'SAME_REQUEST_NUMBER must emit one shared code; '
            f'got {len(codes)} distinct values: {codes}'
        )

    def test_family_names_still_per_label(
        self, patient, exam_a, exam_b, lab_admin, make_request,
    ):
        """Same shared barcode, but each row remains distinguishable
        by family_name — operators rely on the family caption to
        sort tubes after printing."""
        _set_settings(mode=LabelNumberingMode.SAME_REQUEST_NUMBER, extras=2)
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id, exam_b.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )

        ordered = list(batch.labels.order_by('label_index'))
        family_names = {lbl.family_name for lbl in ordered}
        assert {'Hematology', 'Biochemistry', ''}.issubset(family_names)

    def test_two_separate_requests_get_distinct_barcodes(
        self, patient, exam_a, exam_b, lab_admin, make_request,
    ):
        """Cross-batch uniqueness still holds even though the
        intra-batch constraint is gone — two separate SAME_REQUEST_NUMBER
        batches each call the allocator once and get different
        sequence values. The locked LabelSequence guarantees this."""
        _set_settings(mode=LabelNumberingMode.SAME_REQUEST_NUMBER, extras=1)
        ar1 = _confirmed(patient, lab_admin, make_request, [exam_a.id])
        ar2 = _confirmed(patient, lab_admin, make_request, [exam_b.id])

        batch1 = RequestLabelService.generate_or_get(
            ar1, lab_admin, make_request(lab_admin),
        )
        batch2 = RequestLabelService.generate_or_get(
            ar2, lab_admin, make_request(lab_admin),
        )
        code1 = batch1.labels.first().barcode_value
        code2 = batch2.labels.first().barcode_value
        assert code1 != code2


# ---------------------------------------------------------------------------
# 3. extra_label_count — pinned at the strategy level
# ---------------------------------------------------------------------------

class TestExtraLabelCount:

    def test_extras_zero_strips_all_extras(
        self, patient, exam_a, exam_b, lab_admin, make_request,
    ):
        """Setting extras=0 produces exactly families count labels —
        no extras row gets created. The "minimal labels" config."""
        _set_settings(extras=0)
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id, exam_b.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        assert batch.label_count == 2
        assert batch.family_count == 2
        # No empty family_name rows — every label is a family label.
        empty_family_count = batch.labels.filter(family_name='').count()
        assert empty_family_count == 0

    def test_extras_five_appends_five_extras_rows(
        self, patient, exam_a, lab_admin, make_request,
    ):
        """Operational scenario: lab wants more spare tubes per
        request. 1 family + 5 extras = 6 labels."""
        _set_settings(extras=5)
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        assert batch.label_count == 6
        # Five empty-family rows ⇒ five extras.
        assert batch.labels.filter(family_name='').count() == 5

    def test_strategy_compute_reads_setting_when_no_explicit_extras(
        self, patient, exam_a, lab_admin, make_request,
    ):
        """``LabelCountStrategy.compute(request)`` (no extras arg)
        must consult lab settings — the strategy is the canonical
        helper used by tests + by the service."""
        _set_settings(extras=7)
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id])
        count, families = LabelCountStrategy.compute(ar)
        assert count == 1 + 7
        assert families == ['Hematology']

    def test_strategy_compute_respects_explicit_extras_argument(
        self, patient, exam_a, lab_admin, make_request,
    ):
        """Explicit ``extra_count=N`` overrides whatever the
        setting says — the service uses this to avoid a second
        ``get_solo`` trip."""
        _set_settings(extras=7)  # would say 7 if read
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id])
        count, _ = LabelCountStrategy.compute(ar, extra_count=3)
        assert count == 1 + 3


# ---------------------------------------------------------------------------
# 4. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_zero_families_with_extras_produces_extras_only_batch(
        self, patient, exam_no_family, lab_admin, make_request,
    ):
        """A request whose only exam carries no family yields 0
        family-labels. With extras > 0 the batch is just the extras
        — every row carries empty family_name."""
        _set_settings(extras=3)
        ar = _confirmed(patient, lab_admin, make_request, [exam_no_family.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        assert batch.label_count == 3
        assert batch.family_count == 0
        # All three labels are extras (empty family_name).
        assert batch.labels.filter(family_name='').count() == 3

    def test_zero_families_zero_extras_refuses_with_validation_error(
        self, patient, exam_no_family, lab_admin, make_request,
    ):
        """No families AND no extras → there's nothing operational
        to print. Refuse rather than create a phantom 0-label
        batch that downstream surfaces (audit, PDF, scan workflow)
        would silently mishandle."""
        _set_settings(extras=0)
        ar = _confirmed(patient, lab_admin, make_request, [exam_no_family.id])
        with pytest.raises(ValidationError):
            RequestLabelService.generate_or_get(
                ar, lab_admin, make_request(lab_admin),
            )

    def test_same_request_number_with_extras_only_works(
        self, patient, exam_no_family, lab_admin, make_request,
    ):
        """SAME_REQUEST_NUMBER + extras-only batch: still allocates
        ONE code, all extras share it."""
        _set_settings(mode=LabelNumberingMode.SAME_REQUEST_NUMBER, extras=2)
        ar = _confirmed(patient, lab_admin, make_request, [exam_no_family.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        codes = {lbl.barcode_value for lbl in batch.labels.all()}
        assert batch.label_count == 2
        assert len(codes) == 1


# ---------------------------------------------------------------------------
# 5. Audit metadata — captures the configured mode + extras
# ---------------------------------------------------------------------------

class TestAuditSnapshot:

    def test_audit_records_numbering_mode_and_extras(
        self, patient, exam_a, lab_admin, make_request,
    ):
        """An audit reader investigating a historical batch must be
        able to reconstruct the exact lab config that produced it.
        The audit row snapshots the three label-related settings."""
        _set_settings(mode=LabelNumberingMode.SAME_REQUEST_NUMBER, extras=4)
        ar = _confirmed(patient, lab_admin, make_request, [exam_a.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )

        rows = list(AuditLog.objects.filter(
            entity_type='RequestLabelBatch', entity_id=batch.id,
            action=AuditAction.CREATE,
        ))
        assert len(rows) == 1
        diff_after = rows[0].diff['after']
        assert diff_after['numbering_mode'] == 'SAME_REQUEST_NUMBER'
        assert diff_after['extra_label_count'] == 4
        assert diff_after['reset_period'] == 'MONTHLY'  # default
        # Pre-existing fields still present — back-compat for any
        # downstream audit consumer.
        assert diff_after['label_count'] == 5  # 1 family + 4 extras
        assert diff_after['family_count'] == 1
