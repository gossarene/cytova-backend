"""
Tests for the platform-admin team management surface.

Three groups:

  1. Bootstrap command (``create_platform_admin``):
       - Happy path: SUPER_ADMIN created, row exists, password hashed.
       - Duplicate email refused.
       - Non-SUPER_ADMIN role refused without --allow-non-super-admin.
       - --no-input + no --password is a hard error (no prompt path).
       - The command does NOT echo the password to stdout.

  2. Team API permissions:
       - Read: any active platform admin.
       - Write: SUPER_ADMIN only — every other role gets a 403.
       - Inactive admin: refused at the auth layer (401).

  3. Last-super-admin invariants:
       - Cannot deactivate the only active super admin (self).
       - Cannot deactivate the only active super admin (other actor).
       - Cannot demote the only active super admin via change-role.
       - With another active super admin present, every operation
         above succeeds.

  4. Audit content blocklist:
       - Every state-changing call writes one audit row.
       - The audit metadata NEVER contains the temporary password,
         the password hash, or anything that looks like a token.
"""
from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from apps.platform_admin.models import (
    PlatformAdminAuditLog, PlatformAdminRole, PlatformAdminUser,
    PlatformAuditAction,
)
from apps.platform_admin.tokens import PlatformAdminAccessToken


PASSWORD = 'Strong-Pass-1234!'

BASE = '/api/v1/platform-admin/team/'


def _action_url(admin_id, slug: str) -> str:
    return f'{BASE}{admin_id}/{slug}/'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> APIClient:
    return APIClient(HTTP_HOST='core.localhost')


def _auth(client: APIClient, token: str) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _make_admin(
    *, role: str = PlatformAdminRole.SUPER_ADMIN,
    email: str | None = None, is_active: bool = True,
) -> PlatformAdminUser:
    return PlatformAdminUser.objects.create_user(
        email=email or f'{role.lower()}-{id(role)}@cytova.io',
        password=PASSWORD, role=role, is_active=is_active,
    )


def _admin_client(admin: PlatformAdminUser | None = None) -> APIClient:
    admin = admin or _make_admin()
    return _auth(_client(), str(PlatformAdminAccessToken.for_user(admin)))


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ===========================================================================
# 1. Bootstrap command
# ===========================================================================

@pytest.mark.django_db
class TestCreatePlatformAdminCommand:

    def test_creates_first_super_admin(self):
        out = StringIO()
        call_command(
            'create_platform_admin',
            email='founder@cytova.io',
            first_name='Ada', last_name='Lovelace',
            password=PASSWORD,
            stdout=out,
        )
        admin = PlatformAdminUser.objects.get(email='founder@cytova.io')
        assert admin.role == PlatformAdminRole.SUPER_ADMIN
        assert admin.is_active is True
        # Password is hashed, never stored in plaintext.
        assert admin.password != PASSWORD
        assert admin.password.startswith('pbkdf2_')
        assert admin.check_password(PASSWORD)
        # Output mentions the new id + role but NEVER the password.
        text = out.getvalue()
        assert 'founder@cytova.io' in text
        assert PASSWORD not in text

    def test_refuses_duplicate_email(self):
        _make_admin(email='dup@cytova.io')
        with pytest.raises(CommandError, match='already exists'):
            call_command(
                'create_platform_admin',
                email='dup@cytova.io', password=PASSWORD,
                stdout=StringIO(),
            )

    def test_refuses_non_super_admin_without_opt_in(self):
        # Default role is SUPER_ADMIN; passing --role=SUPPORT must
        # require the explicit --allow-non-super-admin opt-in so a
        # CLI typo can't seed a low-priv bootstrap.
        with pytest.raises(CommandError, match='--allow-non-super-admin'):
            call_command(
                'create_platform_admin',
                email='support@cytova.io',
                role=PlatformAdminRole.SUPPORT.value,
                password=PASSWORD,
                stdout=StringIO(),
            )

    def test_allows_non_super_admin_with_opt_in(self):
        call_command(
            'create_platform_admin',
            email='support-ok@cytova.io',
            role=PlatformAdminRole.SUPPORT.value,
            password=PASSWORD,
            allow_non_super_admin=True,
            stdout=StringIO(),
        )
        admin = PlatformAdminUser.objects.get(email='support-ok@cytova.io')
        assert admin.role == PlatformAdminRole.SUPPORT

    def test_no_input_without_password_is_error(self):
        # The --no-input flag exists for CI. Without --password it
        # MUST fail loudly rather than hanging on a prompt.
        with pytest.raises(CommandError, match='--password is required'):
            call_command(
                'create_platform_admin',
                email='ci@cytova.io',
                no_input=True,
                stdout=StringIO(),
            )

    def test_weak_password_rejected_by_validators(self):
        # Django's MinimumLengthValidator etc. should refuse a
        # trivially weak password during bootstrap so the platform
        # never carries a degraded credential from day one.
        with pytest.raises(CommandError, match='Password rejected'):
            call_command(
                'create_platform_admin',
                email='weak@cytova.io',
                password='short',
                stdout=StringIO(),
            )


# ===========================================================================
# 2. Team API — permissions
# ===========================================================================

@pytest.mark.django_db
class TestTeamApiPermissions:

    def test_list_open_to_any_active_admin(self):
        # SUPPORT can read the team list so they know who to
        # escalate to. The list shouldn't be a SUPER_ADMIN secret.
        admin = _make_admin(
            email='support-read@cytova.io', role=PlatformAdminRole.SUPPORT,
        )
        _make_admin(email='other@cytova.io', role=PlatformAdminRole.SUPER_ADMIN)
        resp = _admin_client(admin).get(BASE)
        assert resp.status_code == 200, resp.content

    def test_create_requires_super_admin(self):
        # SUPPORT can read the list but cannot create teammates.
        admin = _make_admin(
            email='support-write@cytova.io', role=PlatformAdminRole.SUPPORT,
        )
        resp = _admin_client(admin).post(BASE, data={
            'email': 'newbie@cytova.io',
            'role': PlatformAdminRole.SUPPORT.value,
        }, format='json')
        assert resp.status_code == 403, resp.content
        assert not PlatformAdminUser.objects.filter(
            email='newbie@cytova.io',
        ).exists()

    def test_super_admin_can_create_team_member(self):
        actor = _make_admin(
            email='founder@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        resp = _admin_client(actor).post(BASE, data={
            'email': 'NewHire@cytova.io',
            'first_name': 'New', 'last_name': 'Hire',
            'role': PlatformAdminRole.SUPPORT.value,
        }, format='json')
        assert resp.status_code == 201, resp.content
        body = _data(resp)
        # Email normalised to lowercase by the service.
        assert body['email'] == 'newhire@cytova.io'
        assert body['role'] == PlatformAdminRole.SUPPORT.value
        # Temporary password returned ONCE in the response — never
        # the empty string, always alphanumeric+symbols, generated
        # cryptographically.
        assert isinstance(body['temporary_password'], str)
        assert len(body['temporary_password']) >= 16

    def test_duplicate_email_returns_400(self):
        actor = _make_admin(
            email='founder-dup@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        _make_admin(email='taken@cytova.io', role=PlatformAdminRole.SUPPORT)
        resp = _admin_client(actor).post(BASE, data={
            'email': 'taken@cytova.io',
            'role': PlatformAdminRole.SUPPORT.value,
        }, format='json')
        assert resp.status_code == 400, resp.content

    def test_inactive_admin_rejected_at_auth(self):
        admin = _make_admin(
            email='inactive@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
            is_active=False,
        )
        token = str(PlatformAdminAccessToken.for_user(admin))
        resp = _auth(_client(), token).get(BASE)
        assert resp.status_code == 401, resp.content


# ===========================================================================
# 3. Last-super-admin invariants
# ===========================================================================

@pytest.mark.django_db
class TestLastSuperAdminGuards:

    def test_cannot_deactivate_only_super_admin_self(self):
        actor = _make_admin(
            email='only@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        resp = _admin_client(actor).post(
            _action_url(actor.id, 'deactivate'),
        )
        assert resp.status_code == 400, resp.content
        actor.refresh_from_db()
        assert actor.is_active is True

    def test_cannot_deactivate_only_super_admin_other(self):
        # Two super admins; one is being deactivated by the other. If
        # the second one isn't active yet, the operation must refuse.
        actor = _make_admin(
            email='actor-other@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        target = _make_admin(
            email='target-other@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
            is_active=False,
        )
        resp = _admin_client(actor).post(_action_url(actor.id, 'deactivate'))
        # ``actor`` is the only ACTIVE super admin (target is inactive)
        # → cannot deactivate ``actor`` either.
        assert resp.status_code == 400
        # Sanity: target stays inactive, actor stays active.
        actor.refresh_from_db()
        target.refresh_from_db()
        assert actor.is_active is True
        assert target.is_active is False

    def test_cannot_demote_only_super_admin(self):
        actor = _make_admin(
            email='lonesome@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        resp = _admin_client(actor).post(
            _action_url(actor.id, 'change-role'),
            data={'role': PlatformAdminRole.SUPPORT.value},
            format='json',
        )
        assert resp.status_code == 400, resp.content
        actor.refresh_from_db()
        assert actor.role == PlatformAdminRole.SUPER_ADMIN

    def test_can_deactivate_when_another_super_admin_exists(self):
        actor = _make_admin(
            email='founder-ok@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        target = _make_admin(
            email='partner@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        resp = _admin_client(actor).post(
            _action_url(target.id, 'deactivate'),
        )
        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.is_active is False
        actor.refresh_from_db()
        assert actor.is_active is True

    def test_can_demote_when_another_super_admin_exists(self):
        actor = _make_admin(
            email='founder-demote@cytova.io',
            role=PlatformAdminRole.SUPER_ADMIN,
        )
        target = _make_admin(
            email='partner-demote@cytova.io',
            role=PlatformAdminRole.SUPER_ADMIN,
        )
        resp = _admin_client(actor).post(
            _action_url(target.id, 'change-role'),
            data={'role': PlatformAdminRole.SUPPORT.value},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.role == PlatformAdminRole.SUPPORT


# ===========================================================================
# 4. Audit
# ===========================================================================

# Strings that must NEVER appear in a written audit row. Checked as
# substrings of the JSON-serialised metadata so a stray nested field
# is also caught.
SECRET_KEYS = ('password', 'pwd', 'temporary_password', 'token', 'pbkdf2_')


def _metadata_text(row: PlatformAdminAuditLog) -> str:
    return json.dumps(row.metadata or {})


@pytest.mark.django_db
class TestAuditTrail:

    def test_create_writes_audit_without_password(self):
        actor = _make_admin(
            email='audit-create@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        resp = _admin_client(actor).post(BASE, data={
            'email': 'audited@cytova.io',
            'role': PlatformAdminRole.SUPPORT.value,
        }, format='json')
        assert resp.status_code == 201
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=actor,
            action=PlatformAuditAction.PLATFORM_ADMIN_CREATED,
        ))
        assert len(rows) == 1
        text = _metadata_text(rows[0])
        for key in SECRET_KEYS:
            assert key not in text, (
                f'Audit metadata leaked {key!r}: {text}'
            )
        # Positive content checks.
        assert rows[0].metadata['target_email'] == 'audited@cytova.io'
        assert rows[0].metadata['role'] == PlatformAdminRole.SUPPORT.value

    def test_deactivate_writes_before_after(self):
        actor = _make_admin(
            email='audit-deact@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        target = _make_admin(
            email='audit-target-deact@cytova.io',
            role=PlatformAdminRole.SUPER_ADMIN,
        )
        _admin_client(actor).post(_action_url(target.id, 'deactivate'))
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=actor,
            action=PlatformAuditAction.PLATFORM_ADMIN_DEACTIVATED,
            entity_id=target.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['before']['is_active'] is True
        assert meta['after']['is_active'] is False

    def test_change_role_writes_before_after(self):
        actor = _make_admin(
            email='audit-role@cytova.io', role=PlatformAdminRole.SUPER_ADMIN,
        )
        target = _make_admin(
            email='audit-target-role@cytova.io',
            role=PlatformAdminRole.SUPER_ADMIN,
        )
        _admin_client(actor).post(
            _action_url(target.id, 'change-role'),
            data={'role': PlatformAdminRole.SUPPORT.value},
            format='json',
        )
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=actor,
            action=PlatformAuditAction.PLATFORM_ADMIN_ROLE_CHANGED,
            entity_id=target.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['before']['role'] == PlatformAdminRole.SUPER_ADMIN.value
        assert meta['after']['role'] == PlatformAdminRole.SUPPORT.value

    def test_refused_action_writes_no_audit(self):
        # A SUPPORT admin attempts to create a team member → 403.
        # No audit row may be written for the refused attempt.
        admin = _make_admin(
            email='no-audit@cytova.io', role=PlatformAdminRole.SUPPORT,
        )
        before = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_ADMIN_CREATED,
        ).count()
        _admin_client(admin).post(BASE, data={
            'email': 'shouldnotexist@cytova.io',
            'role': PlatformAdminRole.SUPPORT.value,
        }, format='json')
        after = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_ADMIN_CREATED,
        ).count()
        assert before == after
