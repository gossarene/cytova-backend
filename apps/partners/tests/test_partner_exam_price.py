"""
Cytova — PartnerExamPrice tests.

Covers the full lifecycle of the partner-specific agreed pricing reference:
list / create / retrieve / partial_update / deactivate / reactivate, plus
the business invariants (uniqueness, immutability of the (partner, exam)
pair, audit logging, permissions, and historical integrity on price
changes).

Tests hit the HTTP layer via APIClient so the router wiring, permission
class, serializer coherence and service delegation are all exercised in
one pass. Unit-level coverage of the service methods in isolation is
unnecessary here — the integration path is the one the UI actually uses.
"""
from datetime import date
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import (
    ExamDefinition, ExamFamily, SampleType,
)
from apps.partners.models import (
    OrganizationType, PartnerExamPrice, PartnerOrganization,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    """Same pattern as the catalog HTTP tests — the middleware rejects
    tenant requests without a usable subscription, so we attach a trial
    to the session tenant in the public schema."""
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
def partner():
    return PartnerOrganization.objects.create(
        code='ACME',
        name='ACME Clinic',
        organization_type=OrganizationType.CLINIC,
    )


@pytest.fixture()
def other_partner():
    return PartnerOrganization.objects.create(
        code='BETA',
        name='Beta Hospital',
        organization_type=OrganizationType.HOSPITAL,
    )


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def exam(family):
    return ExamDefinition.objects.create(
        family=family,
        code='CBC',
        name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def other_exam(family):
    return ExamDefinition.objects.create(
        family=family,
        code='GLU',
        name='Glucose',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('30.0000'),
    )


API = '/api/v1/partners'


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# List / retrieve
# ---------------------------------------------------------------------------

class TestList:

    def test_list_returns_only_partner_scoped_rows(
        self, admin_client, partner, other_partner, exam,
    ):
        own = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        foreign = PartnerExamPrice.objects.create(
            partner=other_partner, exam_definition=exam, agreed_price=Decimal('43.0000'),
        )

        resp = admin_client.get(f'{API}/{partner.id}/exam-prices/')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(own.id) in ids
        assert str(foreign.id) not in ids

    def test_list_includes_display_fields(self, admin_client, partner, exam):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.get(f'{API}/{partner.id}/exam-prices/')
        row = _data(resp)[0]
        assert row['exam_definition_id'] == str(exam.id)
        assert row['exam_code'] == 'CBC'
        assert row['exam_name'] == 'Complete Blood Count'
        assert row['reference_unit_price'] == '50.0000'
        assert row['agreed_price'] == '42.0000'
        assert row['partner_code'] == 'ACME'
        assert row['partner_name'] == 'ACME Clinic'
        assert row['is_active'] is True

    def test_list_allowed_for_non_admin(self, tech_client, partner, exam):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = tech_client.get(f'{API}/{partner.id}/exam-prices/')
        assert resp.status_code == 200

    def test_retrieve(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.get(f'{API}/{partner.id}/exam-prices/{price.id}/')
        assert resp.status_code == 200
        assert _data(resp)['agreed_price'] == '42.0000'

    def test_retrieve_cross_partner_404(
        self, admin_client, partner, other_partner, exam,
    ):
        """Price rows are scoped to their partner — fetching another
        partner's row through a different partner's URL must 404."""
        price = PartnerExamPrice.objects.create(
            partner=other_partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.get(f'{API}/{partner.id}/exam-prices/{price.id}/')
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreate:

    def test_create(self, admin_client, partner, exam):
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/',
            {
                'exam_definition_id': str(exam.id),
                'agreed_price': '45.0000',
                'notes': 'Q1 2026 negotiation',
            },
            format='json',
        )
        assert resp.status_code == 201, resp.content
        body = _data(resp)
        assert body['agreed_price'] == '45.0000'
        assert body['exam_code'] == 'CBC'
        assert body['reference_unit_price'] == '50.0000'  # reference unchanged
        assert PartnerExamPrice.objects.filter(
            partner=partner, exam_definition=exam, agreed_price=Decimal('45.0000'),
        ).exists()

    def test_create_rejects_duplicate_active_pair(self, admin_client, partner, exam):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/',
            {'exam_definition_id': str(exam.id), 'agreed_price': '50.0000'},
            format='json',
        )
        assert resp.status_code == 400
        errors = resp.json().get('errors', [])
        assert any(e.get('field') == 'exam_definition_id' for e in errors), errors

    def test_create_allows_same_exam_if_previous_deactivated(
        self, admin_client, partner, exam,
    ):
        """Deactivating an old agreed price must free the (partner, exam)
        slot so a fresh negotiation can be recorded without losing the
        history of the previous row."""
        old = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam,
            agreed_price=Decimal('42.0000'), is_active=False,
        )
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/',
            {'exam_definition_id': str(exam.id), 'agreed_price': '48.0000'},
            format='json',
        )
        assert resp.status_code == 201, resp.content
        # Both rows exist; one active, one historical.
        rows = PartnerExamPrice.objects.filter(
            partner=partner, exam_definition=exam,
        )
        assert rows.count() == 2
        assert rows.filter(is_active=True).count() == 1
        assert rows.filter(is_active=False).count() == 1
        assert rows.get(pk=old.pk).is_active is False

    def test_create_rejects_inactive_exam(self, admin_client, partner, exam):
        exam.is_active = False
        exam.save()
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/',
            {'exam_definition_id': str(exam.id), 'agreed_price': '10.0000'},
            format='json',
        )
        assert resp.status_code == 400

    def test_create_rejects_negative_price(self, admin_client, partner, exam):
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/',
            {'exam_definition_id': str(exam.id), 'agreed_price': '-1.0000'},
            format='json',
        )
        assert resp.status_code == 400

    def test_create_forbidden_for_non_admin(self, tech_client, partner, exam):
        resp = tech_client.post(
            f'{API}/{partner.id}/exam-prices/',
            {'exam_definition_id': str(exam.id), 'agreed_price': '10.0000'},
            format='json',
        )
        assert resp.status_code == 403

    def test_create_writes_audit(self, admin_client, partner, exam):
        before = AuditLog.objects.filter(entity_type='PartnerExamPrice').count()
        admin_client.post(
            f'{API}/{partner.id}/exam-prices/',
            {'exam_definition_id': str(exam.id), 'agreed_price': '42.0000'},
            format='json',
        )
        after = AuditLog.objects.filter(entity_type='PartnerExamPrice').count()
        assert after == before + 1


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class TestUpdate:

    def test_update_agreed_price(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.patch(
            f'{API}/{partner.id}/exam-prices/{price.id}/',
            {'agreed_price': '55.0000'},
            format='json',
        )
        assert resp.status_code == 200
        price.refresh_from_db()
        assert price.agreed_price == Decimal('55.0000')

    def test_update_notes(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.patch(
            f'{API}/{partner.id}/exam-prices/{price.id}/',
            {'notes': 'Adjusted after contract renewal.'},
            format='json',
        )
        assert resp.status_code == 200
        price.refresh_from_db()
        assert price.notes == 'Adjusted after contract renewal.'

    def test_update_rejects_partner_change(
        self, admin_client, partner, other_partner, exam,
    ):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.patch(
            f'{API}/{partner.id}/exam-prices/{price.id}/',
            {'partner_id': str(other_partner.id)},
            format='json',
        )
        assert resp.status_code == 400

    def test_update_rejects_exam_change(
        self, admin_client, partner, exam, other_exam,
    ):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.patch(
            f'{API}/{partner.id}/exam-prices/{price.id}/',
            {'exam_definition_id': str(other_exam.id)},
            format='json',
        )
        assert resp.status_code == 400

    def test_update_forbidden_for_non_admin(self, tech_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = tech_client.patch(
            f'{API}/{partner.id}/exam-prices/{price.id}/',
            {'agreed_price': '99.0000'},
            format='json',
        )
        assert resp.status_code == 403

    def test_update_writes_audit(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        before = AuditLog.objects.filter(
            entity_type='PartnerExamPrice', action='UPDATE', entity_id=price.id,
        ).count()
        admin_client.patch(
            f'{API}/{partner.id}/exam-prices/{price.id}/',
            {'agreed_price': '55.0000'},
            format='json',
        )
        after = AuditLog.objects.filter(
            entity_type='PartnerExamPrice', action='UPDATE', entity_id=price.id,
        ).count()
        assert after == before + 1


# ---------------------------------------------------------------------------
# Deactivate / reactivate
# ---------------------------------------------------------------------------

class TestLifecycle:

    def test_deactivate(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/{price.id}/deactivate/',
        )
        assert resp.status_code == 200
        price.refresh_from_db()
        assert price.is_active is False

    def test_reactivate(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam,
            agreed_price=Decimal('42.0000'), is_active=False,
        )
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/{price.id}/reactivate/',
        )
        assert resp.status_code == 200
        price.refresh_from_db()
        assert price.is_active is True

    def test_reactivate_rejects_active_conflict(
        self, admin_client, partner, exam,
    ):
        """Cannot reactivate a row when another active row already exists
        for the same (partner, exam) pair — otherwise the DB unique
        constraint would raise IntegrityError."""
        old = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam,
            agreed_price=Decimal('42.0000'), is_active=False,
        )
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam,
            agreed_price=Decimal('55.0000'), is_active=True,
        )
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/{old.id}/reactivate/',
        )
        assert resp.status_code == 400
        old.refresh_from_db()
        assert old.is_active is False

    def test_deactivate_idempotent(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam,
            agreed_price=Decimal('42.0000'), is_active=False,
        )
        before = AuditLog.objects.filter(
            entity_type='PartnerExamPrice', action='DEACTIVATE', entity_id=price.id,
        ).count()
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/{price.id}/deactivate/',
        )
        assert resp.status_code == 200
        after = AuditLog.objects.filter(
            entity_type='PartnerExamPrice', action='DEACTIVATE', entity_id=price.id,
        ).count()
        assert after == before  # no duplicate audit row

    def test_reactivate_idempotent(self, admin_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        before = AuditLog.objects.filter(
            entity_type='PartnerExamPrice', action='REACTIVATE', entity_id=price.id,
        ).count()
        resp = admin_client.post(
            f'{API}/{partner.id}/exam-prices/{price.id}/reactivate/',
        )
        assert resp.status_code == 200
        after = AuditLog.objects.filter(
            entity_type='PartnerExamPrice', action='REACTIVATE', entity_id=price.id,
        ).count()
        assert after == before

    def test_deactivate_forbidden_for_non_admin(self, tech_client, partner, exam):
        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )
        resp = tech_client.post(
            f'{API}/{partner.id}/exam-prices/{price.id}/deactivate/',
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Historical integrity — reference agreed price changes must not
# retroactively touch existing request items.
# ---------------------------------------------------------------------------

class TestHistoricalIntegrity:

    def test_agreed_price_update_does_not_affect_existing_request_items(
        self, admin_client, partner, exam, lab_admin,
    ):
        """
        The AnalysisRequestItem data model snapshots ``billed_price`` at
        creation time, so updating a PartnerExamPrice later cannot change
        any historical item. We assert this directly so a future refactor
        that tries to normalise pricing through a JOIN would break the
        test immediately.
        """
        from apps.patients.models import Patient
        from apps.requests.models import AnalysisRequest, AnalysisRequestItem

        price = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('42.0000'),
        )

        patient = Patient.objects.create(
            document_number='TST-HIST-PEP',
            first_name='Historical',
            last_name='Request',
            date_of_birth=date(1990, 1, 1),
            gender='MALE',
        )
        req = AnalysisRequest.objects.create(
            patient=patient,
            created_by=lab_admin,
        )
        item = AnalysisRequestItem.objects.create(
            analysis_request=req,
            exam_definition=exam,
            unit_price=Decimal('50.0000'),
            billed_price=Decimal('42.0000'),  # snapshotted from the agreed price
        )

        # Renegotiate — bump the agreed price aggressively.
        resp = admin_client.patch(
            f'{API}/{partner.id}/exam-prices/{price.id}/',
            {'agreed_price': '999.0000'},
            format='json',
        )
        assert resp.status_code == 200
        price.refresh_from_db()
        assert price.agreed_price == Decimal('999.0000')

        # The historical item is untouched.
        item.refresh_from_db()
        assert item.billed_price == Decimal('42.0000')
        assert item.unit_price == Decimal('50.0000')
