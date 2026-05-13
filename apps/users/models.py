import uuid
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone


class Role(models.TextChoices):
    LAB_ADMIN = 'LAB_ADMIN', 'Lab Admin'
    BIOLOGIST = 'BIOLOGIST', 'Biologist'
    TECHNICIAN = 'TECHNICIAN', 'Technician'
    RECEPTIONIST = 'RECEPTIONIST', 'Receptionist'
    BILLING_OFFICER = 'BILLING_OFFICER', 'Billing Officer'
    INVENTORY_MANAGER = 'INVENTORY_MANAGER', 'Inventory Manager'
    VIEWER_AUDITOR = 'VIEWER_AUDITOR', 'Viewer / Auditor'


# Roles that only platform admins can assign (never delegated to tenant users)
PLATFORM_ONLY_ROLES: frozenset[str] = frozenset()  # No tenant role is platform-only

# All tenant-level role values for validation
TENANT_ROLES: frozenset[str] = frozenset(r.value for r in Role)


#: Smart defaults for the per-user notification flags, keyed by
#: role. Applied at creation time by ``StaffUserManager.create_user``
#: ONLY when the caller didn't explicitly pass a value — roles are
#: a suggestion, never the final authority.
_ROLE_NOTIFICATION_DEFAULTS: dict[str, dict[str, bool]] = {
    Role.BIOLOGIST: {'receive_review_ready_notifications': True},
    Role.LAB_ADMIN: {'receive_review_ready_notifications': True},
    Role.TECHNICIAN: {'receive_result_rejection_notifications': True},
}


class StaffUserManager(BaseUserManager):
    """Custom manager for StaffUser using email as the unique identifier."""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('An email address is required.')
        email = self.normalize_email(email)

        # Apply role-derived notification defaults BEFORE constructing
        # the model so the operator can still override them by passing
        # explicit kwargs (e.g. ``receive_review_ready_notifications=False``
        # for a biologist who doesn't want the blast). The defaults
        # never overwrite an already-supplied value.
        role = extra_fields.get('role')
        for field, value in _ROLE_NOTIFICATION_DEFAULTS.get(role, {}).items():
            extra_fields.setdefault(field, value)

        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role', Role.LAB_ADMIN)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self.create_user(email, password, **extra_fields)


class StaffUser(AbstractBaseUser, PermissionsMixin):
    """
    Per-tenant laboratory staff user. Lives in each tenant's private schema.

    Used as AUTH_USER_MODEL. Email is the login identifier; role drives RBAC.
    Extends PermissionsMixin for Django admin compatibility.

    Note: PermissionsMixin introduces ManyToMany relations to auth.Group and
    auth.Permission, which live in the public schema. These cross-schema
    relations work correctly because the DB search_path includes 'public'.
    They are not used for Cytova's RBAC — our Role field handles access control.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    title = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text='Professional title (e.g. "Dr", "Pr"). Displayed on '
                  'signed documents such as final reports.',
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(
        max_length=30,
        blank=True,
        default='',
        help_text='Contact phone number, free format. Collected at onboarding '
                  'for the lab admin; optional for other staff.',
    )
    role = models.CharField(max_length=30, choices=Role.choices)
    signature_file_key = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Internal storage key for the user\'s signature image. '
                  'Rendered on reports validated by this user.',
    )

    # -- Internal-workflow notification preferences --
    # Per-user opt-in flags for the two internal email channels.
    # Role-based "smart defaults" are applied at creation time
    # via ``StaffUserManager.create_user`` — see that method's
    # docstring for the mapping. After creation, the LAB_ADMIN
    # can flip these flags manually for any user; roles are NOT
    # the final authority on who receives emails.
    receive_review_ready_notifications = models.BooleanField(
        default=False,
        help_text='If True, this user receives the "request ready for '
                  'biological validation" email when a request becomes '
                  'reviewable.',
    )
    receive_result_rejection_notifications = models.BooleanField(
        default=False,
        help_text='If True, this user receives the "your submitted '
                  'result was rejected" email when a biologist rejects '
                  'a result they entered.',
    )

    # Django internals
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)   # Django admin access

    # Self-referential: who created this user (null for the first Lab Admin)
    created_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_users',
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name', 'role']

    objects = StaffUserManager()

    class Meta:
        verbose_name = 'Staff User'
        verbose_name_plural = 'Staff Users'
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f'{self.get_full_name()} <{self.email}>'

    def get_full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    def get_display_name(self):
        """Backwards-compatible alias for ``professional_display_name``.
        Returns the title-prefixed name (e.g. "Dr René GOSSA") so existing
        callers — notably the result PDF renderer's signature block —
        keep working unchanged. New code should prefer the explicit
        ``display_name`` / ``professional_display_name`` properties.
        """
        return self.professional_display_name

    @property
    def display_name(self):
        """Human-readable name without title — "René GOSSA". Falls back
        to the email when no name parts are populated yet (e.g. an
        invited user who hasn't filled their profile)."""
        name = self.get_full_name()
        return name or self.email

    @property
    def professional_display_name(self):
        """Title-prefixed name for medical / signature contexts —
        "Dr René GOSSA". Falls back to ``display_name`` when no title
        is set. Used by report PDFs and validator attribution."""
        base = self.display_name
        if self.title:
            return f'{self.title} {base}'
        return base

    def get_short_name(self):
        return self.first_name

    @property
    def is_lab_admin(self):
        return self.role == Role.LAB_ADMIN

    def has_perm_code(self, code: str) -> bool:
        """Check if this user has a specific permission code."""
        from common.permission_checker import PermissionChecker
        return PermissionChecker.has_permission(self, code)


class OverrideType(models.TextChoices):
    GRANT = 'GRANT', 'Grant'
    REVOKE = 'REVOKE', 'Revoke'


class UserPermissionOverride(models.Model):
    """
    Per-user permission override within a tenant.

    Allows granting permissions beyond the user's role defaults,
    or revoking specific permissions from the role defaults.

    Managed by lab_admin only. Every change is audit-logged.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        StaffUser,
        on_delete=models.CASCADE,
        related_name='permission_overrides',
    )
    permission_code = models.CharField(max_length=80, db_index=True)
    override_type = models.CharField(max_length=10, choices=OverrideType.choices)
    granted_by = models.ForeignKey(
        StaffUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name='granted_overrides',
    )
    reason = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'User Permission Override'
        verbose_name_plural = 'User Permission Overrides'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'permission_code'],
                name='unique_user_permission_override',
            ),
        ]

    def __str__(self):
        return f'{self.override_type} {self.permission_code} for {self.user.email}'


class PasswordResetToken(models.Model):
    """
    Single-use password reset token for staff users. Per-tenant.

    The plaintext token is sent to the user via email and never persisted.
    Only the SHA-256 hash is stored (via hash_token() utility).
    Tokens expire after 1 hour and are invalidated on use.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        StaffUser,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens',
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set when the token is consumed; complements is_used for audit/forensics.',
    )
    created_by_ip = models.GenericIPAddressField(
        null=True, blank=True,
        help_text='IP address of the requester at token creation time. '
                  'Useful for forensic review of password-reset abuse.',
    )

    class Meta:
        verbose_name = 'Password Reset Token'
        verbose_name_plural = 'Password Reset Tokens'

    def __str__(self):
        return f'PasswordResetToken for {self.user.email} (used={self.is_used})'
