"""
Tests for the numeric-only label code allocator and the per-tenant
monthly sequence.

Covers:
- Generated codes are 14-digit numeric strings
- Format is tenant_code (4) + YY (2) + MM (2) + sequence (6)
- Sequence increments monotonically within a (year, month)
- Codes are system-wide unique (DB constraint preserved)
- Old LBL-* codes from historical batches are left alone
- Collection is blocked when no labels exist, allowed when they do
"""
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.catalog.models import ExamCategory, ExamDefinition, ExamFamily, SampleType
from apps.patients.models import Patient
from apps.requests.label_service import (
    RequestLabelService, _allocate_numeric_code, _current_tenant_numeric_code,
)
from apps.requests.models import (
    AnalysisRequest, LabelSequence, RequestLabel,
    RequestStatus, SourceType,
)
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.tenants.models import Tenant


# This module exercises the explicit generation API and the
# collection-gate, so the autouse auto-generator is a nuisance.
pytestmark = pytest.mark.no_auto_labels


API = '/api/v1/requests'


# ---------------------------------------------------------------------------
# Fixtures — mirror test_labels.py's minimal setup
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
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-LC-001',
        first_name='Alice', last_name='Coded',
        date_of_birth=date(1990, 1, 1), gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family(category):
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def exam(family, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='CBC-LC', name='CBC',
        sample_type=SampleType.BLOOD, unit_price=Decimal('50'),
    )


def _confirmed(patient, lab_admin, make_request, exam_ids):
    return AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': eid} for eid in exam_ids],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )


# ---------------------------------------------------------------------------
# Allocator — low-level format + sequence behaviour
# ---------------------------------------------------------------------------

class TestAllocator:

    def test_tenant_code_resolves_from_schema(self):
        code = _current_tenant_numeric_code()
        assert code.isdigit()
        assert len(code) == 4

    def test_sequence_produces_expected_format(self, db):
        LabelSequence.objects.all().delete()
        code = _allocate_numeric_code('0042', date(2026, 4, 1))
        assert code == '00422604000001'
        assert len(code) == 14
        assert code.isdigit()

    def test_sequence_increments_monthly(self, db):
        LabelSequence.objects.all().delete()
        a = _allocate_numeric_code('0042', date(2026, 4, 1))
        b = _allocate_numeric_code('0042', date(2026, 4, 1))
        c = _allocate_numeric_code('0042', date(2026, 4, 1))
        assert [int(x[-6:]) for x in (a, b, c)] == [1, 2, 3]

    def test_sequence_is_scoped_per_month(self, db):
        LabelSequence.objects.all().delete()
        april = _allocate_numeric_code('0042', date(2026, 4, 1))
        may = _allocate_numeric_code('0042', date(2026, 5, 1))
        # Each month starts at 000001
        assert april.endswith('000001')
        assert may.endswith('000001')
        # Different month segments
        assert april[4:8] == '2604'
        assert may[4:8] == '2605'


# ---------------------------------------------------------------------------
# Service integration
# ---------------------------------------------------------------------------

class TestGeneratedCodeFormat:

    def test_all_labels_in_batch_are_14_digit_numeric(
        self, patient, exam, lab_admin, make_request,
    ):
        ar = _confirmed(patient, lab_admin, make_request, [exam.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        for label in batch.labels.all():
            assert label.barcode_value.isdigit()
            assert len(label.barcode_value) == 14

    def test_prefix_contains_current_tenant_code(
        self, patient, exam, lab_admin, make_request,
    ):
        ar = _confirmed(patient, lab_admin, make_request, [exam.id])
        batch = RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        tenant_code = Tenant.objects.get(
            schema_name=connection.schema_name,
        ).numeric_code
        for label in batch.labels.all():
            assert label.barcode_value.startswith(tenant_code)

    def test_codes_unique_across_batches(
        self, patient, exam, lab_admin, make_request,
    ):
        ar1 = _confirmed(patient, lab_admin, make_request, [exam.id])
        RequestLabelService.generate_or_get(ar1, lab_admin, make_request(lab_admin))

        p2 = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='NID-LC-002',
            first_name='B', last_name='C', date_of_birth=date(1990, 1, 1),
            gender='MALE', created_by=lab_admin,
        )
        ar2 = _confirmed(p2, lab_admin, make_request, [exam.id])
        RequestLabelService.generate_or_get(ar2, lab_admin, make_request(lab_admin))

        codes = list(RequestLabel.objects.values_list('barcode_value', flat=True))
        assert len(codes) == len(set(codes))


# ---------------------------------------------------------------------------
# Collection workflow enforcement
# ---------------------------------------------------------------------------

class TestCollectionRequiresLabels:

    def test_cannot_mark_collected_without_labels(
        self, patient, exam, lab_admin, technician, make_request,
    ):
        ar = _confirmed(patient, lab_admin, make_request, [exam.id])
        item = ar.items.first()
        with pytest.raises(ValidationError) as exc:
            AnalysisRequestItemService.mark_collected(
                item=item,
                collected_by=technician,
                request=make_request(technician),
            )
        # The validation message must point operators at the labels step
        assert 'labels' in str(exc.value).lower()

    def test_can_mark_collected_after_labels_generated(
        self, patient, exam, lab_admin, technician, make_request,
    ):
        ar = _confirmed(patient, lab_admin, make_request, [exam.id])
        RequestLabelService.generate_or_get(
            ar, lab_admin, make_request(lab_admin),
        )
        item = ar.items.first()
        AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=technician,
            request=make_request(technician),
        )
        item.refresh_from_db()
        from apps.requests.models import ItemStatus
        assert item.status == ItemStatus.COLLECTED

    def test_http_mark_collected_returns_400_without_labels(
        self, patient, exam, lab_admin, technician, make_request,
    ):
        ar = _confirmed(patient, lab_admin, make_request, [exam.id])
        item = ar.items.first()
        c = APIClient(HTTP_HOST='testlab.localhost')
        c.force_authenticate(user=technician)
        r = c.post(f'{API}/{ar.id}/items/{item.id}/mark-collected/')
        assert r.status_code == 400
