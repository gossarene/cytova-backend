"""
Tests for the ``PatientAccount`` model — primarily the password hashing
contract. The signup service is exercised separately in
``test_services.py``.
"""
from __future__ import annotations

import pytest

from apps.patient_portal.models import PatientAccount


@pytest.mark.django_db(transaction=True)
class TestPatientAccount:

    def test_password_is_hashed_not_stored_plain(self):
        plain = 'super-secret-pw-91!'
        account = PatientAccount.objects.create_user(
            email='hash@portal.test', password=plain,
        )
        assert account.password != plain
        # Django's hashers prefix the format identifier — pbkdf2_sha256
        # is the project default. Asserting on the prefix means this
        # test catches an accidental switch to plaintext storage even
        # if someone changes the default hasher.
        assert account.password.startswith('pbkdf2_'), account.password
        assert account.check_password(plain) is True
        assert account.check_password('wrong') is False

    def test_email_normalised_to_lowercase(self):
        account = PatientAccount.objects.create_user(
            email='Mixed.Case@Portal.Test', password='x' * 12,
        )
        assert account.email == 'mixed.case@portal.test'
