"""
Tests for the safe-edit contract on ``ExamDefinition.result_structure``.

A lab admin must be able to correct a mis-typed exam structure (e.g.
the receptionist created CBC as SINGLE_VALUE by accident) without:

  - breaking in-flight requests already using the old structure
  - hard-deleting existing parameters
  - dropping the exam code uniqueness guard

The contract under test:

  1. ``ExamDefinitionService.change_structure`` flips the structure
     on the catalog row, optionally seeding parameters on the
     SINGLE_VALUE → MULTI_PARAMETER path and soft-deactivating
     existing parameters on the reverse path.

  2. Existing ``AnalysisRequestItem`` rows keep behaving as they
     did at creation time, because their
     ``result_structure_snapshot`` and ``parameter_ids_snapshot``
     fields freeze the catalog state at that moment. Verified at
     two layers:

     - Submission completeness check
       (``ResultVersionService.submit``) reads the snapshot, not
       the live exam.

     - Report rendering (``_collect_sections``) returns the
       snapshotted structure on each exam dict.

  3. New requests created AFTER the structure flip use the new
     structure and the new parameter scope. Parameters added /
     deactivated post-creation only affect future requests.

  4. Every structural mutation writes one audit row whose ``diff``
     records the before / after structure plus counters; no patient
     data is recorded.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamParameter,
    ExamTechnique, ResultStructure, SampleType,
)
from apps.catalog.services import ExamDefinitionService, ExamParameterService
from apps.patients.models import Patient
from apps.requests.item_structure import (
    effective_active_parameter_ids, effective_result_structure,
)
from apps.requests.models import SourceType
from apps.requests.report_service import _collect_sections
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.models import ResultStatus
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
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Cytometry')


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Default', display_order=1)


@pytest.fixture()
def single_exam(category, family, technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=technique,
        code='WBC-EDIT', name='White Cells',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='10^3/uL', reference_range='4.5-11.0',
        unit_price=Decimal('40'),
    )


@pytest.fixture()
def multi_exam(category, family, technique):
    exam = ExamDefinition.objects.create(
        category=category, family=family, technique=technique,
        code='CBC-EDIT', name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.MULTI_PARAMETER,
        unit_price=Decimal('80'),
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='WBC', name='WBC',
        unit='10^3/uL', reference_range='4.5-11.0', display_order=1,
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='HGB', name='Hemoglobin',
        unit='g/dL', reference_range='12.0-16.0', display_order=2,
    )
    return exam


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-STRUCT-001',
        first_name='Sara', last_name='Sample',
        date_of_birth=date(1990, 1, 1), gender='FEMALE',
        created_by=lab_admin,
    )


def _create_confirmed_request(patient, lab_admin, technician, biologist,
                              make_request, exam):
    """Create → confirm → collect — leaves the item ready for result entry."""
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
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician,
            request=make_request(technician),
        )
    return ar


# ===========================================================================
# 1. Structure transitions
# ===========================================================================

@pytest.mark.django_db
class TestStructureTransitions:

    def test_single_to_multi_succeeds(
        self, single_exam, lab_admin, make_request,
    ):
        ExamDefinitionService.change_structure(
            exam=single_exam,
            new_structure=ResultStructure.MULTI_PARAMETER,
            parameters=[
                {'code': 'WBC', 'name': 'WBC',
                 'unit': '10^3/uL', 'reference_range': '4.5-11.0'},
                {'code': 'HGB', 'name': 'Hemoglobin',
                 'unit': 'g/dL', 'reference_range': '12.0-16.0'},
            ],
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        single_exam.refresh_from_db()
        assert single_exam.result_structure == ResultStructure.MULTI_PARAMETER
        # SINGLE-only fields are cleared so a confused mix doesn't
        # confuse downstream renderers.
        assert single_exam.unit == ''
        assert single_exam.reference_range == ''
        # Both parameters are persisted and active.
        codes = list(
            single_exam.parameters
            .filter(is_active=True)
            .values_list('code', flat=True)
        )
        assert set(codes) == {'WBC', 'HGB'}

    def test_multi_to_single_succeeds_without_deleting_parameters(
        self, multi_exam, lab_admin, make_request,
    ):
        existing_ids = set(multi_exam.parameters.values_list('id', flat=True))
        assert len(existing_ids) == 2

        ExamDefinitionService.change_structure(
            exam=multi_exam,
            new_structure=ResultStructure.SINGLE_VALUE,
            parameters=None,
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        multi_exam.refresh_from_db()
        assert multi_exam.result_structure == ResultStructure.SINGLE_VALUE
        # Parameters are NOT deleted — they're flipped inactive so
        # historical ``ResultValue`` rows that PROTECT-FK them
        # stay readable.
        surviving = set(multi_exam.parameters.values_list('id', flat=True))
        assert surviving == existing_ids
        assert multi_exam.parameters.filter(is_active=True).count() == 0

    def test_same_structure_is_noop_no_audit(
        self, single_exam, lab_admin, make_request,
    ):
        before_audit = AuditLog.objects.filter(
            entity_type='ExamDefinition',
        ).count()
        ExamDefinitionService.change_structure(
            exam=single_exam,
            new_structure=ResultStructure.SINGLE_VALUE,
            parameters=None,
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        after_audit = AuditLog.objects.filter(
            entity_type='ExamDefinition',
        ).count()
        assert before_audit == after_audit

    def test_single_to_multi_without_parameters_rejected(
        self, single_exam, lab_admin, make_request,
    ):
        with pytest.raises(ValidationError):
            ExamDefinitionService.change_structure(
                exam=single_exam,
                new_structure=ResultStructure.MULTI_PARAMETER,
                parameters=[],
                updated_by=lab_admin,
                request=make_request(lab_admin),
            )
        single_exam.refresh_from_db()
        # Structure is unchanged after a rejected attempt.
        assert single_exam.result_structure == ResultStructure.SINGLE_VALUE

    def test_invalid_structure_rejected(
        self, single_exam, lab_admin, make_request,
    ):
        with pytest.raises(ValidationError):
            ExamDefinitionService.change_structure(
                exam=single_exam,
                new_structure='SOMETHING_ELSE',
                parameters=None,
                updated_by=lab_admin,
                request=make_request(lab_admin),
            )

    def test_code_uniqueness_still_enforced(
        self, single_exam, multi_exam,
    ):
        # Even after changing structure, the code uniqueness invariant
        # holds at the DB level. A second exam with the same code
        # cannot be created — proves the constraint stayed in place.
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            ExamDefinition.objects.create(
                category=single_exam.category,
                family=single_exam.family,
                technique=single_exam.technique,
                code='WBC-EDIT',  # duplicate of single_exam.code
                name='Duplicate', sample_type=SampleType.BLOOD,
                result_structure=ResultStructure.SINGLE_VALUE,
            )


# ===========================================================================
# 2. Parameter lifecycle (no hard delete)
# ===========================================================================

@pytest.mark.django_db
class TestParameterLifecycle:

    def test_add_parameter_succeeds(
        self, multi_exam, lab_admin, make_request,
    ):
        param = ExamParameterService.create(
            exam=multi_exam,
            validated_data={
                'code': 'PLT', 'name': 'Platelets',
                'unit': '10^3/uL', 'reference_range': '150-400',
                'display_order': 3,
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert param.is_active is True
        assert multi_exam.parameters.filter(code='PLT').exists()

    def test_deactivate_parameter_keeps_row(
        self, multi_exam, lab_admin, make_request,
    ):
        param = multi_exam.parameters.get(code='WBC')
        param_id = param.id
        ExamParameterService.deactivate(
            param=param,
            deactivated_by=lab_admin,
            request=make_request(lab_admin),
        )
        param.refresh_from_db()
        assert param.is_active is False
        # Row still exists — no hard delete.
        assert ExamParameter.objects.filter(pk=param_id).exists()

    def test_parameter_hard_delete_blocked(self, multi_exam):
        param = multi_exam.parameters.first()
        with pytest.raises(PermissionError):
            param.delete()

    def test_inactive_parameter_excluded_from_future_snapshot(
        self, multi_exam, lab_admin, technician, biologist,
        patient, make_request,
    ):
        # Deactivate one parameter on the catalog.
        param = multi_exam.parameters.get(code='HGB')
        ExamParameterService.deactivate(
            param=param, deactivated_by=lab_admin,
            request=make_request(lab_admin),
        )
        # A new request created AFTER deactivation must NOT carry
        # the deactivated parameter in its snapshot.
        ar = _create_confirmed_request(
            patient, lab_admin, technician, biologist,
            make_request, multi_exam,
        )
        item = ar.items.get()
        assert effective_active_parameter_ids(item) == [
            str(multi_exam.parameters.get(code='WBC').id),
        ]


# ===========================================================================
# 3. Snapshot safety — existing requests don't follow catalog edits
# ===========================================================================

@pytest.mark.django_db
class TestSnapshotSafety:

    def test_existing_single_request_stays_single_after_flip(
        self, single_exam, lab_admin, technician, biologist,
        patient, make_request,
    ):
        # Create a SINGLE_VALUE request first.
        ar = _create_confirmed_request(
            patient, lab_admin, technician, biologist,
            make_request, single_exam,
        )
        item = ar.items.get()
        assert effective_result_structure(item) == ResultStructure.SINGLE_VALUE
        assert item.result_structure_snapshot == ResultStructure.SINGLE_VALUE

        # Flip the catalog to MULTI_PARAMETER.
        ExamDefinitionService.change_structure(
            exam=single_exam,
            new_structure=ResultStructure.MULTI_PARAMETER,
            parameters=[
                {'code': 'NEW1', 'name': 'New Param',
                 'unit': '', 'reference_range': ''},
            ],
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        single_exam.refresh_from_db()
        assert single_exam.result_structure == ResultStructure.MULTI_PARAMETER

        # The existing item still sees its SINGLE structure.
        item.refresh_from_db()
        assert effective_result_structure(item) == ResultStructure.SINGLE_VALUE
        # And the result-entry service accepts a SINGLE_VALUE entry
        # on this item — proving the snapshot drives entry, not the
        # live catalog.
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician,
            request=make_request(technician),
            result_value='7.2',
            values=[{'value': '7.2', 'is_abnormal': False}],
        )
        ResultVersionService.submit(
            version=v, submitted_by=technician,
            request=make_request(technician),
        )
        v.refresh_from_db()
        assert v.status == ResultStatus.SUBMITTED

    def test_new_request_after_flip_uses_new_structure(
        self, single_exam, lab_admin, technician, biologist,
        patient, make_request,
    ):
        # Flip first, then create the request.
        ExamDefinitionService.change_structure(
            exam=single_exam,
            new_structure=ResultStructure.MULTI_PARAMETER,
            parameters=[
                {'code': 'P1', 'name': 'P1', 'unit': '', 'reference_range': ''},
                {'code': 'P2', 'name': 'P2', 'unit': '', 'reference_range': ''},
            ],
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        single_exam.refresh_from_db()
        ar = _create_confirmed_request(
            patient, lab_admin, technician, biologist,
            make_request, single_exam,
        )
        item = ar.items.get()
        assert effective_result_structure(item) == ResultStructure.MULTI_PARAMETER
        assert len(effective_active_parameter_ids(item)) == 2

    def test_existing_multi_request_stays_multi_after_flip(
        self, multi_exam, lab_admin, technician, biologist,
        patient, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, technician, biologist,
            make_request, multi_exam,
        )
        item = ar.items.get()
        param_ids_at_creation = sorted(
            effective_active_parameter_ids(item)
        )
        assert len(param_ids_at_creation) == 2

        # Flip the catalog to SINGLE_VALUE — parameters are
        # soft-deactivated.
        ExamDefinitionService.change_structure(
            exam=multi_exam,
            new_structure=ResultStructure.SINGLE_VALUE,
            parameters=None,
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )

        # The existing item still believes it is MULTI_PARAMETER,
        # and the parameter snapshot is unchanged.
        item.refresh_from_db()
        assert effective_result_structure(item) == ResultStructure.MULTI_PARAMETER
        assert sorted(effective_active_parameter_ids(item)) == param_ids_at_creation

        # Submitting a complete multi-param result still works —
        # the snapshot says "these are the parameters you need".
        params = list(multi_exam.parameters.all())
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician,
            request=make_request(technician),
            values=[
                {'parameter_id': str(p.id), 'value': '7.0', 'is_abnormal': False}
                for p in params
            ],
        )
        ResultVersionService.submit(
            version=v, submitted_by=technician,
            request=make_request(technician),
        )
        v.refresh_from_db()
        assert v.status == ResultStatus.SUBMITTED

    def test_added_parameter_does_not_enter_in_flight_request(
        self, multi_exam, lab_admin, technician, biologist,
        patient, make_request,
    ):
        # Request is created with 2 parameters.
        ar = _create_confirmed_request(
            patient, lab_admin, technician, biologist,
            make_request, multi_exam,
        )
        item = ar.items.get()
        ids_before = sorted(effective_active_parameter_ids(item))
        assert len(ids_before) == 2

        # Lab adds a third parameter to the catalog.
        new_param = ExamParameterService.create(
            exam=multi_exam,
            validated_data={
                'code': 'PLT', 'name': 'Platelets',
                'unit': '10^3/uL', 'reference_range': '150-400',
                'display_order': 3,
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        # The existing item's snapshot still has only 2 ids — the
        # new parameter does not retroactively become required on
        # the in-flight item.
        item.refresh_from_db()
        ids_after = sorted(effective_active_parameter_ids(item))
        assert ids_after == ids_before
        assert str(new_param.id) not in ids_after

    def test_report_renders_snapshot_structure(
        self, single_exam, lab_admin, technician, biologist,
        patient, make_request,
    ):
        ar = _create_confirmed_request(
            patient, lab_admin, technician, biologist,
            make_request, single_exam,
        )
        item = ar.items.get()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician,
            request=make_request(technician),
            result_value='8.0',
            values=[{'value': '8.0', 'is_abnormal': False}],
        )
        ResultVersionService.submit(
            version=v, submitted_by=technician,
            request=make_request(technician),
        )
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(
            version=v, validation_notes='OK', validated_by=biologist,
            request=make_request(biologist),
        )
        ar.refresh_from_db()
        AnalysisRequestService.finalize_validation(
            analysis_request=ar, finalized_by=biologist,
            request=make_request(biologist),
        )
        ar.refresh_from_db()

        # Flip the catalog AFTER finalisation.
        ExamDefinitionService.change_structure(
            exam=single_exam,
            new_structure=ResultStructure.MULTI_PARAMETER,
            parameters=[
                {'code': 'NEW', 'name': 'New', 'unit': '', 'reference_range': ''},
            ],
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )

        sections = _collect_sections(ar)
        # The exam dict surfaced to the renderer carries the
        # snapshot structure, NOT the live structure.
        assert sections[0]['exams'][0]['structure'] == ResultStructure.SINGLE_VALUE


# ===========================================================================
# 4. Audit
# ===========================================================================

@pytest.mark.django_db
class TestAudit:

    def test_structure_change_writes_audit_row(
        self, single_exam, lab_admin, make_request,
    ):
        ExamDefinitionService.change_structure(
            exam=single_exam,
            new_structure=ResultStructure.MULTI_PARAMETER,
            parameters=[
                {'code': 'P1', 'name': 'P1', 'unit': '', 'reference_range': ''},
            ],
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        rows = list(AuditLog.objects.filter(
            entity_type='ExamDefinition',
            entity_id=single_exam.id,
        ).order_by('-timestamp'))
        assert rows, 'Structure change must write an audit row.'
        latest = rows[0]
        diff = latest.diff
        assert diff['structure_change'] is True
        assert diff['before']['result_structure'] == ResultStructure.SINGLE_VALUE
        assert diff['after']['result_structure'] == ResultStructure.MULTI_PARAMETER
        assert diff['parameters_added'] == 1
        assert diff['parameters_deactivated'] == 0
        assert diff['exam_code'] == single_exam.code

    def test_multi_to_single_audit_counts_deactivations(
        self, multi_exam, lab_admin, make_request,
    ):
        ExamDefinitionService.change_structure(
            exam=multi_exam,
            new_structure=ResultStructure.SINGLE_VALUE,
            parameters=None,
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        latest = AuditLog.objects.filter(
            entity_type='ExamDefinition',
            entity_id=multi_exam.id,
        ).order_by('-timestamp').first()
        assert latest.diff['structure_change'] is True
        assert latest.diff['parameters_deactivated'] == 2

    def test_parameter_lifecycle_writes_audit(
        self, multi_exam, lab_admin, make_request,
    ):
        # Add
        ExamParameterService.create(
            exam=multi_exam,
            validated_data={
                'code': 'AUDIT', 'name': 'Audited Param',
                'unit': '', 'reference_range': '',
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        # Deactivate
        param = multi_exam.parameters.get(code='AUDIT')
        ExamParameterService.deactivate(
            param=param, deactivated_by=lab_admin,
            request=make_request(lab_admin),
        )
        actions = list(AuditLog.objects.filter(
            entity_type='ExamParameter', entity_id=param.id,
        ).values_list('action', flat=True))
        assert 'CREATE' in actions
        assert 'DEACTIVATE' in actions

    def test_audit_metadata_contains_no_patient_data(
        self, multi_exam, lab_admin, patient, technician, biologist, make_request,
    ):
        # Create a request first to ensure a Patient row exists.
        _create_confirmed_request(
            patient, lab_admin, technician, biologist,
            make_request, multi_exam,
        )
        ExamDefinitionService.change_structure(
            exam=multi_exam,
            new_structure=ResultStructure.SINGLE_VALUE,
            parameters=None,
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        latest = AuditLog.objects.filter(
            entity_type='ExamDefinition', entity_id=multi_exam.id,
        ).order_by('-timestamp').first()
        import json
        text = json.dumps(latest.diff)
        # Pin: no patient names / document numbers / DOB years
        # ever land in catalog audit metadata.
        for forbidden in (
            patient.first_name, patient.last_name,
            patient.document_number, '1990',
        ):
            assert forbidden not in text
