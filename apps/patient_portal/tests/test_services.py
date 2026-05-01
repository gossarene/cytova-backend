"""
End-to-end tests for ``register_patient_account`` — the signup service
that the future HTTP endpoint and the test suite both call into.
"""
from __future__ import annotations

from datetime import date

import pytest
from django.test import override_settings
from rest_framework.exceptions import ValidationError

from apps.patient_portal.models import (
    PatientAccount, PatientConsent, PatientProfile,
)
from apps.patient_portal.services import register_patient_account


def _payload(**overrides):
    base = dict(
        email='new@portal.test',
        password='strong-pw-1234!',
        first_name='Ada',
        last_name='Lovelace',
        date_of_birth=date(1990, 5, 17),
        accept_terms=True,
    )
    base.update(overrides)
    return base


class _FakeRequest:
    """Stand-in for the audit-context-enriched request the middleware
    attaches in production."""
    audit_ip = '203.0.113.42'
    audit_user_agent = 'Mozilla/5.0 (test)'


@pytest.mark.django_db(transaction=True)
class TestRegisterPatientAccount:

    def test_creates_account_profile_and_consent(self):
        account = register_patient_account(**_payload(), request=_FakeRequest())

        assert account.pk is not None
        # Profile created with a generated ID — assert the format
        # guarantee (full coverage of the format lives in the
        # id_generator suite).
        profile = PatientProfile.objects.get(account=account)
        assert profile.cytova_patient_id.startswith('CV-')
        assert profile.first_name == 'Ada'
        # Consent row created and reachable from the account.
        consents = list(account.consents.all())
        assert len(consents) == 1

    def test_terms_required_raises_before_writing(self):
        with pytest.raises(ValidationError) as exc:
            register_patient_account(**_payload(accept_terms=False))
        assert 'accept_terms' in exc.value.detail
        # Critical: nothing should have been persisted on a refused
        # signup. A leaky transaction here would mean an account exists
        # without a consent row — exactly the audit gap we're guarding
        # against.
        assert not PatientAccount.objects.filter(email='new@portal.test').exists()
        assert not PatientProfile.objects.exists()
        assert not PatientConsent.objects.exists()

    @override_settings(
        PATIENT_TERMS_VERSION='v2', PATIENT_PRIVACY_VERSION='v3',
    )
    def test_consent_snapshots_settings_versions_and_request_metadata(self):
        account = register_patient_account(
            **_payload(email='consent@portal.test'),
            request=_FakeRequest(),
        )
        consent = account.consents.get()
        # Snapshots are taken at signup time — a future settings bump
        # must NOT silently rewrite this row.
        assert consent.terms_version == 'v2'
        assert consent.privacy_version == 'v3'
        assert consent.ip_address == '203.0.113.42'
        assert consent.user_agent == 'Mozilla/5.0 (test)'
        assert consent.accepted_at is not None

    def test_duplicate_email_rejected_with_field_error(self):
        register_patient_account(**_payload(email='dup@portal.test'))
        with pytest.raises(ValidationError) as exc:
            register_patient_account(**_payload(email='dup@portal.test'))
        assert 'email' in exc.value.detail
