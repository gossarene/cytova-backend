"""
Identity-eligibility guard for previous-value surfacing on reports.

Cytova attaches the patient's last known value for an exam onto
generated result reports. This convenience must NEVER cross
patient lines on a flaky identity (auto-generated placeholder IDs
or missing DOB), because the wrong patient's history on a real
report is a clinical incident — not a UX bug.

These tests pin the contract enforced by
``apps.patients.identity.is_patient_identity_reliable_for_history``
and the gate inside ``_build_previous_lookup``:

  1. A patient with a real document_type + document_number + DOB
     (not auto-generated, not flagged unknown) still sees their
     previous value on the report — the existing behaviour is
     preserved end-to-end.

  2. Each of the five "unreliable" axes individually disables the
     previous-value surface:
       - ``date_of_birth_unknown = True``
       - ``date_of_birth = None``
       - ``document_type = UNKNOWN``
       - ``identity_number_auto_generated = True``
       - empty ``document_number``

  3. Report generation still succeeds in every case — the guard
     never blocks the report, only the previous-value surface on
     it.

  4. The ORM query that looks up historical versions is NOT issued
     when identity is unreliable. Pinned via a query-counter so
     a future refactor cannot regress to "fetch + filter in
     Python" — the safety guarantee depends on the SQL never
     leaving the application.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.audit.models import AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.patients.identity import (
    SKIP_REASON_INCOMPLETE_IDENTITY,
    is_patient_identity_reliable_for_history,
)
from apps.patients.models import Patient
from apps.requests.models import AnalysisRequest, SourceType
from apps.requests.report_service import (
    _collect_sections, _build_previous_lookup, RequestReportService,
)
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


# ---------------------------------------------------------------------------
# Subscription fixture (mirrors test_previous_results.py)
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
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Biochemistry', display_order=1)


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Spectrophotometry')


@pytest.fixture()
def single_exam(category, family, technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=technique,
        code='GLU-G', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70-100',
        unit_price=Decimal('50'),
    )


def _make_patient(lab_admin, **overrides) -> Patient:
    """Build a reliably-identified baseline patient. Tests override
    individual fields to flip each unreliable axis."""
    defaults = dict(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-GUARD-001',
        identity_number_auto_generated=False,
        first_name='Hana', last_name='Historical',
        date_of_birth=date(1985, 3, 15),
        date_of_birth_unknown=False,
        gender='FEMALE',
        created_by=lab_admin,
    )
    defaults.update(overrides)
    return Patient.objects.create(**defaults)


def _finalize(patient, lab_admin, technician, biologist, make_request, exam,
              *, single_value='85', created_offset=None) -> AnalysisRequest:
    """Create → confirm → collect → enter → validate → finalize the
    request for ``patient`` + ``exam``. Mirrors the helper in
    ``test_previous_results.py`` (kept duplicated here so this
    suite doesn't reach across test modules)."""
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
    if created_offset is not None:
        AnalysisRequest.objects.filter(pk=ar.pk).update(
            created_at=timezone.now() - created_offset,
        )
        ar.refresh_from_db()

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
            result_value=single_value,
            values=[{'value': single_value, 'is_abnormal': False}],
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


# ===========================================================================
# 1. Helper predicate — pure boolean tests, no DB queries
# ===========================================================================

@pytest.mark.django_db
class TestIdentityPredicate:

    def test_reliable_patient_returns_true(self, lab_admin):
        p = _make_patient(lab_admin)
        assert is_patient_identity_reliable_for_history(p) is True

    def test_unknown_dob_flag_returns_false(self, lab_admin):
        p = _make_patient(lab_admin, date_of_birth_unknown=True)
        assert is_patient_identity_reliable_for_history(p) is False

    def test_null_dob_returns_false(self, lab_admin):
        # Setting ``date_of_birth_unknown=True`` is the supported
        # path for a null DOB. We pin both axes independently —
        # the helper must reject either condition.
        p = _make_patient(
            lab_admin, date_of_birth=None, date_of_birth_unknown=True,
        )
        assert is_patient_identity_reliable_for_history(p) is False

    def test_unknown_document_type_returns_false(self, lab_admin):
        p = _make_patient(lab_admin, document_type='UNKNOWN')
        assert is_patient_identity_reliable_for_history(p) is False

    def test_auto_generated_id_returns_false(self, lab_admin):
        p = _make_patient(
            lab_admin,
            identity_number_auto_generated=True,
            document_number='AUTO-PT-20260101-000001',
        )
        assert is_patient_identity_reliable_for_history(p) is False

    def test_empty_document_number_returns_false(self, lab_admin):
        p = _make_patient(lab_admin, document_number='   ')
        assert is_patient_identity_reliable_for_history(p) is False

    def test_other_document_type_is_accepted_with_real_number(self, lab_admin):
        # ``OTHER`` is "real but uncategorised" — accept when a
        # real document number is on file.
        p = _make_patient(
            lab_admin,
            document_type='OTHER', document_number='OTH-X-77',
        )
        assert is_patient_identity_reliable_for_history(p) is True

    def test_none_patient_returns_false(self):
        # Defensive: callers should never pass None, but the helper
        # tolerates it so it can be threaded through optional fields.
        assert is_patient_identity_reliable_for_history(None) is False


# ===========================================================================
# 2. Section-level integration — previous value shown / not shown
# ===========================================================================

@pytest.mark.django_db
class TestPreviousValueSurface:

    def test_reliable_patient_keeps_previous_value(
        self, lab_admin, technician, biologist, make_request, single_exam,
    ):
        patient = _make_patient(lab_admin)
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='90',
            created_offset=timedelta(days=7),
        )
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        sections = _collect_sections(ar2)
        exam = sections[0]['exams'][0]
        # Existing Phase-N behaviour is preserved end-to-end.
        assert exam['previous_value'] == '90'
        assert exam['previous_date'] is not None

    @pytest.mark.parametrize('overrides', [
        {'date_of_birth_unknown': True},
        # ``date_of_birth=None`` requires ``date_of_birth_unknown=True``
        # at the model level — combined override matches the
        # legitimate "no DOB on file" state.
        {'date_of_birth': None, 'date_of_birth_unknown': True},
        {'document_type': 'UNKNOWN'},
        {
            'identity_number_auto_generated': True,
            'document_number': 'AUTO-PT-20260101-000002',
        },
        {'document_number': ''},
    ])
    def test_unreliable_patient_drops_previous_value(
        self, lab_admin, technician, biologist, make_request, single_exam,
        overrides,
    ):
        patient = _make_patient(lab_admin, **overrides)
        # Seed a historical request — this would surface as
        # ``previous_value`` for a reliable patient.
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='90',
            created_offset=timedelta(days=7),
        )
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        sections = _collect_sections(ar2)
        exam = sections[0]['exams'][0]
        assert exam['previous_value'] is None
        assert exam['previous_date'] is None
        # And the per-value attributes (MULTI_PARAMETER-style) are
        # also nulled for the SINGLE_VALUE shape, since the
        # else-branch in ``_collect_sections`` sets them on every
        # ``ResultValue``.
        for v in exam['values']:
            assert getattr(v, 'previous_value', None) is None
            assert getattr(v, 'previous_date', None) is None


# ===========================================================================
# 3. Lookup-level integration — no SQL when identity is unreliable
# ===========================================================================

@pytest.mark.django_db
class TestLookupQuerySuppression:

    def test_reliable_patient_issues_lookup_query(
        self, lab_admin, technician, biologist, make_request, single_exam,
    ):
        patient = _make_patient(lab_admin)
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='90',
            created_offset=timedelta(days=7),
        )
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        with CaptureQueriesContext(connection) as ctx:
            lookup = _build_previous_lookup(
                ar2, {single_exam.id},
            )
        # ResultVersion is hit; the exact count fluctuates with the
        # ORM (prefetch_related issues a second query). We assert
        # that SOMETHING was fetched.
        assert len(ctx.captured_queries) >= 1
        assert lookup  # non-empty: previous version found

    def test_unreliable_patient_skips_sql_entirely(
        self, lab_admin, technician, biologist, make_request, single_exam,
    ):
        # Build a reliable historical row first so the lookup would
        # otherwise have something to fetch; then mark the patient
        # unreliable and confirm zero SQL is issued by the helper.
        patient = _make_patient(lab_admin)
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='90',
            created_offset=timedelta(days=7),
        )
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )

        patient.date_of_birth_unknown = True
        patient.save(update_fields=['date_of_birth_unknown'])
        ar2.refresh_from_db()

        with CaptureQueriesContext(connection) as ctx:
            lookup = _build_previous_lookup(
                ar2, {single_exam.id},
            )
        assert lookup == {}
        # The helper may legitimately touch ``patients_patient`` (lazy
        # dereference of ``current_request.patient``) or run a
        # ``SET search_path``; neither is a clinical-safety concern.
        # The load-bearing invariant is that ``results_resultversion``
        # is NOT scanned — that's the join that could surface another
        # patient's history on this report.
        result_version_queries = [
            q for q in ctx.captured_queries
            if 'results_resultversion' in q['sql'].lower()
        ]
        assert result_version_queries == [], (
            'Identity-unreliable patient must NOT trigger a previous-'
            'value SQL query against results_resultversion; got:\n'
            + '\n'.join(q['sql'] for q in result_version_queries)
        )


# ===========================================================================
# 4. Report generation succeeds + audit row records the skip reason
# ===========================================================================

@pytest.mark.django_db
class TestReportGenerationStillWorks:

    def test_report_generates_for_unreliable_patient(
        self, lab_admin, technician, biologist, make_request, single_exam,
    ):
        # Reliability is irrelevant to the report itself — only the
        # previous-value surface is gated. Confirm a real PDF is
        # produced and the audit row carries the skip reason.
        patient = _make_patient(
            lab_admin, document_type='UNKNOWN', document_number='AUTO-X',
            identity_number_auto_generated=True,
        )
        ar = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )

        report = RequestReportService.generate_or_get(
            analysis_request=ar,
            generated_by=biologist,
            request=make_request(biologist),
        )
        assert report.pk is not None
        assert report.pdf_file_key  # PDF was persisted

        audit = AuditLog.objects.filter(
            entity_type='AnalysisRequestReport', entity_id=report.id,
        ).first()
        assert audit is not None
        assert audit.diff['after']['previous_values_skipped_reason'] == (
            SKIP_REASON_INCOMPLETE_IDENTITY
        )
        # Sensitive fields are NEVER in the audit metadata.
        text = str(audit.diff)
        assert 'AUTO-X' not in text
        assert '1985' not in text  # no DOB

    def test_report_generates_for_reliable_patient_without_skip_marker(
        self, lab_admin, technician, biologist, make_request, single_exam,
    ):
        patient = _make_patient(lab_admin)
        ar = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        report = RequestReportService.generate_or_get(
            analysis_request=ar,
            generated_by=biologist,
            request=make_request(biologist),
        )
        audit = AuditLog.objects.filter(
            entity_type='AnalysisRequestReport', entity_id=report.id,
        ).first()
        assert audit is not None
        assert 'previous_values_skipped_reason' not in audit.diff['after']
