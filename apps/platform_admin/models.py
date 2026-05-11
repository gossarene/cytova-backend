"""
Cytova — Platform Admin models.

Two models live here:

  - ``PlatformAdminUser`` — the authentication identity for any
    operator working on the Cytova platform itself (separate from
    lab StaffUser and from patient PatientAccount). ``AbstractBaseUser``
    base class so password hashing + ``check_password`` come for free
    via Django's PBKDF2 hasher.

  - ``PlatformAdminAuditLog`` — append-only audit trail for actions
    taken by platform admins. Lives in the public schema next to the
    user model so a single SELECT can join admin → audit. Distinct
    from ``apps.tenants.PlatformAuditLog`` (which audits tenant CRUD
    operations); these two surfaces evolve at different cadences and
    have different retention/PII profiles, so we keep them as
    separate tables.

Naming
------
The user model is ``PlatformAdminUser`` (not just ``PlatformAdmin``)
to avoid colliding with the legacy ``apps.tenants.PlatformAdmin``
scaffold that already exists for the older ``/api/v1/platform/``
tenant-CRUD surface. The two coexist temporarily; consolidation is
out of scope for the foundation phase.
"""
from __future__ import annotations

import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone

from common.utils.serialization import json_safe


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class PlatformAdminRole(models.TextChoices):
    """Platform-admin authorisation roles.

    The five roles map to operational personas inside Cytova rather
    than implementation concerns:

      - ``SUPER_ADMIN``       full control, including managing other
                              platform admins. The "owner" of the
                              platform admin surface.
      - ``SUPPORT``           tenant + user operations on behalf of
                              labs (read tenant state, trigger
                              tenant-side actions). No billing or
                              infra mutations.
      - ``BILLING_ADMIN``     subscription, plan, and invoice-level
                              concerns. Read tenants but cannot
                              suspend/reactivate them.
      - ``TECH_ADMIN``        infrastructure-side operations
                              (feature flags, schema-level
                              maintenance). No billing.
      - ``READ_ONLY_AUDITOR`` everything is read-only. Used for
                              compliance / external auditors who
                              need visibility without write access.

    Concrete authorisation rules per role are out of scope for the
    foundation phase — the role is stored on the user so future
    permission classes can branch on it without a model change.
    """
    SUPER_ADMIN = 'SUPER_ADMIN', 'Super Admin'
    SUPPORT = 'SUPPORT', 'Support'
    BILLING_ADMIN = 'BILLING_ADMIN', 'Billing Admin'
    TECH_ADMIN = 'TECH_ADMIN', 'Tech Admin'
    READ_ONLY_AUDITOR = 'READ_ONLY_AUDITOR', 'Read-only Auditor'


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class PlatformAdminUserManager(BaseUserManager):
    """Manager for ``PlatformAdminUser``.

    Provides ``create_user`` (canonical creation path that hashes the
    password) and ``create_superuser`` (which simply pins the role
    to SUPER_ADMIN — no Django ``is_staff/is_superuser`` flags here
    because this user is NOT registered as ``AUTH_USER_MODEL``).
    """

    def create_user(
        self, email, password=None, *,
        first_name='', last_name='',
        role=PlatformAdminRole.SUPPORT,
        **extra_fields,
    ) -> 'PlatformAdminUser':
        if not email:
            raise ValueError('Platform admin email is required.')
        user = self.model(
            email=self.normalize_email(email),
            first_name=first_name,
            last_name=last_name,
            role=role,
            **extra_fields,
        )
        # set_password runs through Django's configured hasher so the
        # plaintext never lands on disk — this is the load-bearing
        # security guarantee of the model.
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role', PlatformAdminRole.SUPER_ADMIN)
        return self.create_user(email, password, **extra_fields)


class PlatformAdminUser(AbstractBaseUser):
    """Platform-admin identity (public schema only).

    Authentication contract:
      - ``USERNAME_FIELD = 'email'`` so login is by email.
      - ``set_password`` / ``check_password`` come from
        ``AbstractBaseUser`` and use Django's configured hasher
        chain. We never store plaintext.
      - This model is NOT registered as ``AUTH_USER_MODEL``. The
        global default user remains the per-tenant ``StaffUser``;
        platform admins are authenticated via the dedicated
        ``PlatformAdminJWTAuthentication`` class.
      - ``last_login`` is stamped by the login service (not by
        Django's signal-based default, which assumes the user is
        an ``AUTH_USER_MODEL``).

    Lifecycle:
      - ``is_active=False`` immediately blocks login. The login
        service refuses inactive accounts before checking the
        password so a disabled admin cannot use timing differences
        to probe email existence.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100, blank=True, default='')
    last_name = models.CharField(max_length=100, blank=True, default='')
    role = models.CharField(
        max_length=30,
        choices=PlatformAdminRole.choices,
        default=PlatformAdminRole.SUPPORT,
    )
    is_active = models.BooleanField(default=True)

    last_login = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS: list = []

    objects = PlatformAdminUserManager()

    class Meta:
        verbose_name = 'Platform Admin User'
        verbose_name_plural = 'Platform Admin Users'
        ordering = ['email']

    def __str__(self):
        return self.email

    @property
    def full_name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip() or self.email

    @property
    def is_super_admin(self) -> bool:
        return self.role == PlatformAdminRole.SUPER_ADMIN


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class PlatformAuditAction(models.TextChoices):
    """Platform-admin-side audit actions.

    The first wave covers the foundation phase (login event). Future
    phases extend this enum as new admin surfaces are added — the
    enum is the single source of truth for the action taxonomy.
    """
    PLATFORM_ADMIN_LOGIN = 'PLATFORM_ADMIN_LOGIN', 'Platform Admin Login'
    PLATFORM_ADMIN_LOGIN_FAILED = (
        'PLATFORM_ADMIN_LOGIN_FAILED', 'Platform Admin Login Failed',
    )
    PLATFORM_TENANT_LIST_VIEWED = (
        'PLATFORM_TENANT_LIST_VIEWED', 'Platform Tenant List Viewed',
    )
    PLATFORM_TENANT_DETAIL_VIEWED = (
        'PLATFORM_TENANT_DETAIL_VIEWED', 'Platform Tenant Detail Viewed',
    )
    PLATFORM_TENANT_SUSPENDED = (
        'PLATFORM_TENANT_SUSPENDED', 'Platform Tenant Suspended',
    )
    PLATFORM_TENANT_REACTIVATED = (
        'PLATFORM_TENANT_REACTIVATED', 'Platform Tenant Reactivated',
    )
    PLATFORM_TENANT_TRIAL_EXTENDED = (
        'PLATFORM_TENANT_TRIAL_EXTENDED', 'Platform Tenant Trial Extended',
    )
    PLATFORM_TENANT_PLAN_CHANGED = (
        'PLATFORM_TENANT_PLAN_CHANGED', 'Platform Tenant Plan Changed',
    )
    PLATFORM_PATIENT_LIST_VIEWED = (
        'PLATFORM_PATIENT_LIST_VIEWED', 'Platform Patient List Viewed',
    )
    PLATFORM_PATIENT_DETAIL_VIEWED = (
        'PLATFORM_PATIENT_DETAIL_VIEWED', 'Platform Patient Detail Viewed',
    )
    PLATFORM_PATIENT_DEACTIVATED = (
        'PLATFORM_PATIENT_DEACTIVATED', 'Platform Patient Deactivated',
    )
    PLATFORM_PATIENT_REACTIVATED = (
        'PLATFORM_PATIENT_REACTIVATED', 'Platform Patient Reactivated',
    )
    PLATFORM_DASHBOARD_VIEWED = (
        'PLATFORM_DASHBOARD_VIEWED', 'Platform Dashboard Viewed',
    )
    PLATFORM_ADMIN_CREATED = (
        'PLATFORM_ADMIN_CREATED', 'Platform Admin Created',
    )
    PLATFORM_ADMIN_DEACTIVATED = (
        'PLATFORM_ADMIN_DEACTIVATED', 'Platform Admin Deactivated',
    )
    PLATFORM_ADMIN_REACTIVATED = (
        'PLATFORM_ADMIN_REACTIVATED', 'Platform Admin Reactivated',
    )
    PLATFORM_ADMIN_ROLE_CHANGED = (
        'PLATFORM_ADMIN_ROLE_CHANGED', 'Platform Admin Role Changed',
    )


class PlatformAdminAuditLog(models.Model):
    """Append-only audit row for a platform-admin action.

    Why a dedicated table
    ---------------------
    The legacy ``apps.tenants.PlatformAuditLog`` records tenant-CRUD
    actions with an ``actor_email`` string and a ``diff`` JSON. Login
    audits and other admin-account events have different concerns
    (we want a real FK to the actor, a ``user_agent`` line for
    forensic context, and a ``metadata`` payload distinct from the
    tenant-diff shape). A single mixed table would force every
    consumer to filter by action — better to keep two narrow tables.

    Immutability
    ------------
    Append-only is enforced by overriding ``save`` (refuse updates)
    and ``delete`` (refuse all deletes). Tampering is therefore
    visible at the model layer, not just by convention.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # ``actor`` is nullable for two reasons:
    #   1. Failed logins (the email may not match any account, in
    #      which case there's no FK to point at).
    #   2. The user might be deleted later — ``SET_NULL`` keeps the
    #      historical row intact even if the actor row goes away.
    # ``actor_email`` is the immutable email-at-action-time snapshot
    # so audit records survive the actor row's deletion.
    actor = models.ForeignKey(
        PlatformAdminUser,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='audit_logs',
    )
    actor_email = models.EmailField(blank=True, default='')

    action = models.CharField(
        max_length=40,
        choices=PlatformAuditAction.choices,
        db_index=True,
    )
    entity_type = models.CharField(max_length=50, blank=True, default='', db_index=True)
    entity_id = models.UUIDField(null=True, blank=True, db_index=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True, default='')

    # Free-form structured payload. Always coerced through ``json_safe``
    # on save so non-serialisable values (Decimals, datetimes, UUIDs,
    # model instances) are normalised before they hit the JSON column.
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'Platform Admin Audit Log'
        verbose_name_plural = 'Platform Admin Audit Logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action', 'created_at']),
            models.Index(fields=['actor', 'created_at']),
        ]

    def __str__(self):
        return f'[{self.action}] {self.actor_email or "<anonymous>"} @ {self.created_at:%Y-%m-%d %H:%M}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError('Platform admin audit logs are immutable.')
        if self.metadata is not None:
            self.metadata = json_safe(self.metadata)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('Platform admin audit logs cannot be deleted.')
