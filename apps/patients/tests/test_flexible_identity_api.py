"""
Flexible-identity rollout — HTTP-level coverage.

Service- and serializer-layer behaviour is already pinned by
``test_flexible_identity.py``. This file complements that with the
exact request shapes the lab UI sends, so we catch regressions where
the wire format diverges from what the validator expects (e.g.
``date_of_birth=""`` reaching a ``DateField(allow_null=True)`` and
failing with "Date has wrong format" — a classic empty-vs-null
mismatch).

Why HTTP-level
--------------
The wire boundary is where serialisation oddities surface. A
serializer test calling ``serializer.is_valid()`` with a Python
``None`` is not the same as the frontend posting JSON ``null`` or an
empty string; only an APIClient round-trip pins the actual contract
the SPA relies on.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.patients.models import DocumentType, Patient


PATIENTS_URL = '/api/v1/patients/'


# ---------------------------------------------------------------------------
# Subscription + cache fixtures (mirror sibling lab-side suites)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    """Patient endpoints sit behind the trial/subscription gate. The
    sibling test files use the same fixture — keep it identical so
    the patient-create surface here exercises the real auth + gate
    chain rather than a stripped-down view."""
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


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def admin_client(lab_admin):
    client = APIClient(HTTP_HOST='testlab.localhost')
    client.force_authenticate(user=lab_admin)
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOC_SEQ = 0


def _payload(**overrides):
    """Standard create payload — sane defaults that exercise none of
    the new flexible-identity branches. Tests override only the
    fields they care about."""
    global _DOC_SEQ
    _DOC_SEQ += 1
    base = {
        'document_type': 'NATIONAL_ID_CARD',
        'document_number': f'NID-API-{_DOC_SEQ:04d}',
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'date_of_birth': '1990-05-17',
        'gender': 'FEMALE',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Wire format — exact shapes the SPA sends after the flexible-identity
# rollout. These pin the contract that the SPA's payload-normaliser
# relies on (see PatientCreatePage / PatientForm).
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestFlexibleIdentityCreate:

    def test_unknown_doc_with_unknown_dob_succeeds(self, admin_client):
        """The exact wire shape the SPA produces after the
        empty-string fix: ``UNKNOWN`` document type + DOB-unknown
        checked. ``date_of_birth`` is ``null`` (NOT ``""`` — DRF's
        ``DateField(allow_null=True)`` rejects empty strings, so
        the SPA normalises to null at the API boundary)."""
        resp = admin_client.post(
            PATIENTS_URL,
            data=_payload(
                document_type='UNKNOWN',
                document_number='',
                date_of_birth=None,
                date_of_birth_unknown=True,
            ),
            format='json',
        )
        assert resp.status_code == 201, resp.content
        data = resp.json()['data']
        assert data['document_type'] == 'UNKNOWN'
        assert data['identity_number_auto_generated'] is True
        assert data['document_number'].startswith('AUTO-PT-')
        assert data['date_of_birth'] is None
        assert data['date_of_birth_unknown'] is True

        # DB-side check too — the auto-generated number actually
        # landed on the persisted row, not just the response.
        patient = Patient.objects.get(id=data['id'])
        assert patient.identity_number_auto_generated is True
        assert patient.document_number.startswith('AUTO-PT-')

    def test_unknown_doc_with_omitted_document_number_succeeds(self, admin_client):
        """SPA may omit ``document_number`` entirely (rather than
        sending ``""``) when the operator picks UNKNOWN. Both
        wire forms must work — the optional + blank-allowed
        serializer field accepts either."""
        payload = _payload(
            document_type='UNKNOWN',
            date_of_birth=None,
            date_of_birth_unknown=True,
        )
        del payload['document_number']
        resp = admin_client.post(PATIENTS_URL, data=payload, format='json')
        assert resp.status_code == 201, resp.content
        assert resp.json()['data']['identity_number_auto_generated'] is True

    def test_real_type_without_document_number_returns_400(self, admin_client):
        """Case B: a real document type WITHOUT a number is rejected
        at the wire boundary with a field-level error on
        ``document_number``. The SPA branches on the field name —
        a generic 400 wouldn't be useful."""
        resp = admin_client.post(
            PATIENTS_URL,
            data=_payload(
                document_type='PASSPORT',
                document_number='',
            ),
            format='json',
        )
        assert resp.status_code == 400
        # Cytova error envelope: { data: null, meta: null,
        #                          errors: [{code, message, field, detail}] }
        errors = resp.json().get('errors') or []
        assert any(e.get('field') == 'document_number' for e in errors), errors

    def test_dob_unknown_false_with_no_dob_returns_400(self, admin_client):
        """Case D: DOB missing without the explicit unknown flag is
        rejected with a field-level error on ``date_of_birth``.
        This is the safety property of the nullable rollout — a
        forgotten date-picker can never silently land null."""
        resp = admin_client.post(
            PATIENTS_URL,
            data=_payload(
                document_type='NATIONAL_ID_CARD',
                date_of_birth=None,
                date_of_birth_unknown=False,
            ),
            format='json',
        )
        assert resp.status_code == 400
        errors = resp.json().get('errors') or []
        assert any(e.get('field') == 'date_of_birth' for e in errors), errors

    def test_empty_string_dob_rejected_with_field_error(self, admin_client):
        """The reproducer for the original 400 bug: the SPA used to
        send ``date_of_birth=""`` when the operator checked the
        unknown-DOB box, which DRF's ``DateField`` rejected with
        "Date has wrong format". The fix lives on the SPA side (it
        now sends ``null``), but we pin the wire contract here:
        an empty string MUST surface as a clean field error on
        ``date_of_birth``, NOT a 500. The frontend's normalisation
        is what keeps users out of this case in production — this
        test guards against a future regression where the
        normaliser is dropped or bypassed."""
        resp = admin_client.post(
            PATIENTS_URL,
            data=_payload(
                document_type='UNKNOWN',
                document_number='',
                date_of_birth='',
                date_of_birth_unknown=True,
            ),
            format='json',
        )
        # DRF's DateField rejects '' with INVALID_FORMAT — pin that
        # to a clean field-level 400 (NOT a 500, NOT a silent
        # accept).
        assert resp.status_code == 400, resp.content
        errors = resp.json().get('errors') or []
        assert any(e.get('field') == 'date_of_birth' for e in errors), errors
