"""
Platform-admin authentication foundation — phase 1 tests.

Pins every contract listed in the foundation spec:

  - Model behaviour: creation, hashed passwords, role default.
  - Login endpoint: token shape, ``user_type='PLATFORM_ADMIN'``
    claim, last_login bump, audit row.
  - ``/me`` endpoint: works with a platform-admin token; refuses
    lab-staff and patient tokens with 401.
  - Inactive accounts cannot log in.
  - Login audit row written on success (and a parallel failed-login
    audit row on rejection).
  - Schema isolation: the new tables exist in ``public`` and are
    NOT mirrored into any lab tenant schema.

Why these scenarios specifically
--------------------------------
The user-listed checks pin the security boundary of phase 1. The
foundation must prove that:

  (a) Credentials are hashed (not just stored) — anything else is a
      catastrophic breach surface.
  (b) The three audiences (platform admin / lab staff / patient)
      cannot cross authenticate. A token from one stack must NOT
      open the others.
  (c) Disabling an admin immediately blocks logins.
  (d) Every login attempt — successful or failed — is auditable.
  (e) Tenant schemas never carry platform-admin rows. Otherwise
      shared/tenant boundaries would be violated and a tenant
      backup could leak platform-wide credentials.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django_tenants.utils import (
    get_public_schema_name, get_tenant_model, schema_context,
)
from rest_framework.test import APIClient

from apps.patient_portal.services import register_patient_account
from apps.patient_portal.tokens import PatientAccessToken
from apps.platform_admin.models import (
    PlatformAdminAuditLog, PlatformAdminRole, PlatformAdminUser,
    PlatformAuditAction,
)
from apps.platform_admin.tokens import PlatformAdminAccessToken
from apps.users.models import Role, StaffUser
from apps.authentication.tokens import CytovaAccessToken


LOGIN_URL = '/api/v1/platform-admin/auth/login/'
ME_URL = '/api/v1/platform-admin/auth/me/'

PASSWORD = 'Strong-Pass-1234!'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_admin(
    *,
    email: str = 'admin@cytova.io',
    password: str = PASSWORD,
    role: str = PlatformAdminRole.SUPER_ADMIN,
    is_active: bool = True,
    first_name: str = 'Ada',
    last_name: str = 'Lovelace',
) -> PlatformAdminUser:
    return PlatformAdminUser.objects.create_user(
        email=email,
        password=password,
        role=role,
        is_active=is_active,
        first_name=first_name,
        last_name=last_name,
    )


def _client() -> APIClient:
    """APIClient with the platform-admin host so the public-schema
    URL conf serves our routes. Django-tenants resolves the schema
    by host; ``core.localhost`` is not registered as a tenant
    domain, so it falls through to ``PUBLIC_SCHEMA_URLCONF`` —
    exactly what we want."""
    return APIClient(HTTP_HOST='core.localhost')


def _envelope(resp):
    """Envelope unwrapper. The success response from
    ``PlatformAdminLoginView`` is currently a flat dict (not wrapped
    by ``CytovaJSONRenderer`` because the public-schema renderer
    config matches): support both shapes so the test still passes
    if/when the envelope wrapping is added later."""
    body = resp.json()
    return body.get('data', body)


# ===========================================================================
# 1. Model + password hashing
# ===========================================================================

@pytest.mark.django_db
class TestPlatformAdminModel:

    def test_create_user_persists_row(self):
        admin = _make_admin()
        assert PlatformAdminUser.objects.filter(pk=admin.pk).exists()
        assert admin.email == 'admin@cytova.io'
        assert admin.role == PlatformAdminRole.SUPER_ADMIN

    def test_password_is_hashed_not_stored_plaintext(self):
        admin = _make_admin()
        # The stored hash must not be the plaintext, must be
        # checkable, and must follow Django's hasher prefix
        # convention so a future hasher migration ``check_password``
        # path keeps working.
        assert admin.password != PASSWORD
        assert admin.password
        # PBKDF2 / Argon2 / etc all encode the algorithm name as
        # the first ``$``-delimited segment.
        assert '$' in admin.password
        assert admin.check_password(PASSWORD)
        assert not admin.check_password('wrong-password')

    def test_role_default_is_support(self):
        # ``create_user`` sets a role explicitly; the model-level
        # default applies to direct ``.save()`` paths (e.g. admin
        # interface). Pin it so the safe default doesn't drift.
        admin = PlatformAdminUser(email='nobody@cytova.io')
        assert admin.role == PlatformAdminRole.SUPPORT


# ===========================================================================
# 2. Login endpoint
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestLoginEndpoint:

    def test_login_returns_token_with_platform_admin_claims(self):
        admin = _make_admin(role=PlatformAdminRole.SUPER_ADMIN)
        resp = _client().post(
            LOGIN_URL,
            data={'email': admin.email, 'password': PASSWORD},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        body = _envelope(resp)
        assert body['token_type'] == 'Bearer'
        assert body['expires_in'] > 0
        assert body['admin']['email'] == admin.email
        assert body['admin']['role'] == PlatformAdminRole.SUPER_ADMIN

        # Decode the access token and assert the claims that the
        # auth class will read on every subsequent request. We use
        # simplejwt's UntypedToken so the test doesn't care which
        # token subclass produced the JWT — only the claims matter.
        from rest_framework_simplejwt.tokens import UntypedToken
        token = UntypedToken(body['access_token'])
        assert token['user_type'] == 'PLATFORM_ADMIN'
        assert token['role'] == PlatformAdminRole.SUPER_ADMIN
        assert token['email'] == admin.email

    def test_inactive_admin_cannot_login(self):
        admin = _make_admin(is_active=False)
        resp = _client().post(
            LOGIN_URL,
            data={'email': admin.email, 'password': PASSWORD},
            format='json',
        )
        assert resp.status_code == 401, resp.content
        # Generic error code — does not distinguish inactive vs
        # bad password vs unknown email.
        codes = {e['code'] for e in resp.json()['errors']}
        assert codes == {'AUTHENTICATION_FAILED'}

    def test_wrong_password_returns_401(self):
        admin = _make_admin()
        resp = _client().post(
            LOGIN_URL,
            data={'email': admin.email, 'password': 'wrong'},
            format='json',
        )
        assert resp.status_code == 401, resp.content

    def test_unknown_email_returns_401(self):
        resp = _client().post(
            LOGIN_URL,
            data={'email': 'ghost@cytova.io', 'password': PASSWORD},
            format='json',
        )
        assert resp.status_code == 401, resp.content


# ===========================================================================
# 3. /me endpoint + cross-token rejection
# ===========================================================================

def _auth(client, token: str) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


@pytest.mark.django_db(transaction=True)
class TestMeEndpointCrossAudience:

    def test_me_returns_profile_with_platform_admin_token(self):
        admin = _make_admin()
        token = str(PlatformAdminAccessToken.for_user(admin))
        resp = _auth(_client(), token).get(ME_URL)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        # ``/me`` returns the profile shape — pin the keys we plan
        # to consume on the back-office UI.
        assert body['email'] == admin.email
        assert body['role'] == PlatformAdminRole.SUPER_ADMIN
        assert body['id'] == str(admin.id)

    def test_me_rejects_lab_staff_token(self):
        # Build a real staff user inside the lab tenant schema so
        # the token is shaped exactly like a production lab-staff
        # token. We cross the schema boundary explicitly.
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='staff@testlab.io',
                password=PASSWORD,
                first_name='Lab',
                last_name='Admin',
                role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))

        # Hitting /me on the public host with a tenant-staff token
        # must be rejected. Even though the token is signed with
        # the same secret, ``user_type`` is missing — the auth
        # class refuses on that check.
        resp = _auth(_client(), staff_token).get(ME_URL)
        assert resp.status_code == 401, resp.content

    def test_me_rejects_patient_portal_token(self):
        # Patient portal accounts live in the public schema, but
        # the ``user_type='PATIENT'`` claim must still bounce off
        # the platform-admin auth class.
        account = register_patient_account(
            email='patient@portal.test', password=PASSWORD,
            first_name='Pat', last_name='Test',
            date_of_birth='1990-05-17', accept_terms=True,
        )
        from django.utils import timezone
        account.email_verified_at = timezone.now()
        account.save(update_fields=['email_verified_at'])
        patient_token = str(
            PatientAccessToken.for_patient(account, profile=account.profile)
        )

        resp = _auth(_client(), patient_token).get(ME_URL)
        assert resp.status_code == 401, resp.content


# ===========================================================================
# 4. Audit logging
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestLoginAudit:

    def test_successful_login_writes_audit_row(self):
        admin = _make_admin()
        # Pre-condition: no audit yet for this admin.
        assert not PlatformAdminAuditLog.objects.filter(actor=admin).exists()

        resp = _client().post(
            LOGIN_URL,
            data={'email': admin.email, 'password': PASSWORD},
            format='json',
        )
        assert resp.status_code == 200

        rows = list(PlatformAdminAuditLog.objects.filter(actor=admin))
        assert len(rows) == 1
        row = rows[0]
        assert row.action == PlatformAuditAction.PLATFORM_ADMIN_LOGIN
        assert row.actor_email == admin.email
        # Metadata captures the role at-the-time of login so a
        # subsequent role change doesn't rewrite history.
        assert row.metadata['role'] == admin.role

    def test_failed_login_writes_distinct_audit_action(self):
        admin = _make_admin()
        _client().post(
            LOGIN_URL,
            data={'email': admin.email, 'password': 'wrong'},
            format='json',
        )
        # Failed-login audit row exists with the distinct action
        # value so SIEM aggregators can spike-detect on it
        # without false positives from successful logins.
        assert PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_ADMIN_LOGIN_FAILED,
        ).exists()

    def test_unknown_email_logs_with_null_actor_and_email_snapshot(self):
        _client().post(
            LOGIN_URL,
            data={'email': 'ghost@cytova.io', 'password': PASSWORD},
            format='json',
        )
        # No matching user → actor is null, but the attempted email
        # is preserved in ``actor_email`` for the SIEM trail.
        rows = list(PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_ADMIN_LOGIN_FAILED,
            actor__isnull=True,
        ))
        assert len(rows) == 1
        assert rows[0].actor_email == 'ghost@cytova.io'

    def test_successful_login_bumps_last_login(self):
        admin = _make_admin()
        assert admin.last_login is None
        _client().post(
            LOGIN_URL,
            data={'email': admin.email, 'password': PASSWORD},
            format='json',
        )
        admin.refresh_from_db()
        assert admin.last_login is not None


# ===========================================================================
# 5. Schema isolation — public-only tables
# ===========================================================================

PLATFORM_ADMIN_TABLES = (
    'platform_admin_platformadminuser',
    'platform_admin_platformadminauditlog',
)


def _table_exists_in_schema(schema: str, table: str) -> bool:
    qualified = f'{schema}.{table}'
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT to_regclass(%s) IS NOT NULL', [qualified],
        )
        return bool(cursor.fetchone()[0])


@pytest.mark.django_db(transaction=True)
class TestSchemaIsolation:

    def test_tables_exist_in_public_schema(self):
        for table in PLATFORM_ADMIN_TABLES:
            assert _table_exists_in_schema(
                get_public_schema_name(), table,
            ), (
                f'Expected {table} in the public schema. '
                f'apps.platform_admin must be in SHARED_APPS so the '
                f'table is created during ``migrate_schemas --shared``.'
            )

    def test_tables_do_not_exist_in_lab_tenant_schema(self):
        Tenant = get_tenant_model()
        schema = (
            Tenant.objects.exclude(schema_name=get_public_schema_name())
            .values_list('schema_name', flat=True)
            .first()
        )
        assert schema, 'No lab tenant schema available — fix conftest setup.'

        for table in PLATFORM_ADMIN_TABLES:
            assert not _table_exists_in_schema(schema, table), (
                f'Found {table} inside lab tenant schema "{schema}". '
                f'apps.platform_admin must be in SHARED_APPS only — never '
                f'in TENANT_APPS — so platform-admin rows can never be '
                f'reached via a tenant DB connection.'
            )
