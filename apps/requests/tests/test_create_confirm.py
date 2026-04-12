"""
Tests for the atomic create-and-confirm flow used by the 3-step request
creation wizard.

Semantics under test
--------------------
``POST /api/v1/requests/`` with ``confirm=true`` must:

1. Create the request with its inline items (same pricing snapshot as a
   plain draft creation).
2. Transition DRAFT → CONFIRMED via the existing ``RequestStateMachine``.
3. Write both a CREATE and a CONFIRM audit entry.
4. Roll back atomically on any failure — no half-created orphan draft.
5. Reject with a field-scoped 400 if ``items`` is empty when
   ``confirm=true`` (rather than producing a state-machine error
   downstream in ``confirm``).

Backward compat
---------------
``confirm=false`` (or omitted) must continue to produce a DRAFT, leaving
the legacy draft-edit flow untouched.
"""
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import ExamCategory, ExamDefinition, SampleType
from apps.partners.models import OrganizationType, PartnerExamPrice, PartnerOrganization
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest, AnalysisRequestItem, ItemStatus, PriceSource,
    RequestStatus, SourceType, BillingMode,
)
from apps.requests.services import AnalysisRequestService


API = '/api/v1/requests'


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
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-CONF-001',
        first_name='Confirm',
        last_name='Patient',
        date_of_birth='1988-02-02',
        gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Biochemistry', display_order=1)


@pytest.fixture()
def exam(category):
    return ExamDefinition.objects.create(
        category=category,
        code='GLU',
        name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def partner():
    return PartnerOrganization.objects.create(
        code='CNF-CLN',
        name='Confirm Clinic',
        organization_type=OrganizationType.CLINIC,
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# HTTP path — ``confirm=true`` produces a CONFIRMED request atomically
# ---------------------------------------------------------------------------

class TestCreateWithConfirmFlag:

    def test_direct_patient_create_and_confirm(
        self, admin_client, patient, exam,
    ):
        resp = admin_client.post(
            f'{API}/',
            {
                'patient_id': str(patient.id),
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': str(exam.id)}],
                'confirm': True,
            },
            format='json',
        )
        assert resp.status_code == 201, resp.content
        body = _data(resp)
        assert body['status'] == RequestStatus.CONFIRMED
        assert body['confirmed_at'] is not None
        assert body['confirmed_by_email']

        # Items are persisted with the snapshotted pricing from the resolver.
        ar = AnalysisRequest.objects.get(id=body['id'])
        items = list(ar.items.all())
        assert len(items) == 1
        assert items[0].unit_price == Decimal('50.0000')
        assert items[0].billed_price == Decimal('50.0000')
        assert items[0].price_source == PriceSource.DEFAULT_PRICE
        assert items[0].status == ItemStatus.PENDING

    def test_partner_create_and_confirm_uses_agreed_price(
        self, admin_client, patient, exam, partner,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        resp = admin_client.post(
            f'{API}/',
            {
                'patient_id': str(patient.id),
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': str(partner.id),
                'billing_mode': BillingMode.PARTNER_BILLING,
                'items': [{'exam_definition_id': str(exam.id)}],
                'confirm': True,
            },
            format='json',
        )
        assert resp.status_code == 201, resp.content
        body = _data(resp)
        assert body['status'] == RequestStatus.CONFIRMED

        ar = AnalysisRequest.objects.get(id=body['id'])
        item = ar.items.first()
        assert item.billed_price == Decimal('35.0000')
        assert item.price_source == PriceSource.PARTNER_AGREED_PRICE

    def test_confirm_false_still_creates_draft(
        self, admin_client, patient, exam,
    ):
        """Backward-compat: omitting ``confirm`` (or sending false) keeps
        the legacy DRAFT behaviour so draft-edit flows are untouched."""
        resp = admin_client.post(
            f'{API}/',
            {
                'patient_id': str(patient.id),
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': str(exam.id)}],
            },
            format='json',
        )
        assert resp.status_code == 201
        body = _data(resp)
        assert body['status'] == RequestStatus.DRAFT
        assert body['confirmed_at'] is None

    def test_confirm_true_with_no_items_rejected(
        self, admin_client, patient,
    ):
        """Field-scoped 400 at the serializer layer — no half-created
        request should ever hit the DB."""
        before_count = AnalysisRequest.objects.count()
        resp = admin_client.post(
            f'{API}/',
            {
                'patient_id': str(patient.id),
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [],
                'confirm': True,
            },
            format='json',
        )
        assert resp.status_code == 400
        errors = resp.json().get('errors', [])
        assert any(e.get('field') == 'items' for e in errors), errors
        # Atomicity: nothing persisted.
        assert AnalysisRequest.objects.count() == before_count

    def test_confirm_true_writes_both_create_and_confirm_audit_entries(
        self, admin_client, patient, exam,
    ):
        before_create = AuditLog.objects.filter(
            entity_type='AnalysisRequest', action='CREATE',
        ).count()
        before_confirm = AuditLog.objects.filter(
            entity_type='AnalysisRequest', action='CONFIRM',
        ).count()

        resp = admin_client.post(
            f'{API}/',
            {
                'patient_id': str(patient.id),
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': str(exam.id)}],
                'confirm': True,
            },
            format='json',
        )
        assert resp.status_code == 201

        after_create = AuditLog.objects.filter(
            entity_type='AnalysisRequest', action='CREATE',
        ).count()
        after_confirm = AuditLog.objects.filter(
            entity_type='AnalysisRequest', action='CONFIRM',
        ).count()
        assert after_create == before_create + 1
        assert after_confirm == before_confirm + 1


# ---------------------------------------------------------------------------
# Service-level path — same atomicity guarantee without the HTTP layer
# ---------------------------------------------------------------------------

class TestCreateConfirmAtService:

    def test_service_confirm_after_true_produces_confirmed(
        self, patient, exam, lab_admin, make_request,
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
        assert ar.status == RequestStatus.CONFIRMED
        assert ar.confirmed_at is not None
        assert ar.confirmed_by_id == lab_admin.id

    def test_service_default_produces_draft(
        self, patient, exam, lab_admin, make_request,
    ):
        """Omitting ``confirm_after`` preserves the legacy default."""
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert ar.status == RequestStatus.DRAFT
        assert ar.confirmed_at is None

    def test_items_still_snapshotted_when_confirming(
        self, patient, exam, lab_admin, make_request,
    ):
        """Pricing snapshot logic is unchanged by the confirm flag."""
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
        assert item is not None
        assert item.unit_price == Decimal('50.0000')
        assert item.billed_price == Decimal('50.0000')
        assert item.price_source == PriceSource.DEFAULT_PRICE
