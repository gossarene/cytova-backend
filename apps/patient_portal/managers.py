"""
Cytova — Patient Portal account manager.

Mirrors ``BaseUserManager`` but works without being the project's
``AUTH_USER_MODEL`` (that slot is held by ``apps.users.StaffUser``).
``create_user`` is intentionally the only public constructor — the
service layer (``apps.patient_portal.services.register_patient_account``)
handles the full signup flow including profile + consent rows in a
single transaction.
"""
from __future__ import annotations

from django.contrib.auth.models import BaseUserManager


class PatientAccountManager(BaseUserManager):

    use_in_migrations = True

    def create_user(
        self,
        email: str,
        password: str,
        **extra_fields,
    ) -> 'PatientAccount':
        if not email:
            raise ValueError('Email is required.')
        email = self.normalize_email(email).lower()
        account = self.model(email=email, **extra_fields)
        account.set_password(password)
        account.save(using=self._db)
        return account
