import uuid
from datetime import timedelta

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django_tenants.models import TenantMixin, DomainMixin


# ---------------------------------------------------------------------------
# Subscription Plan
# ---------------------------------------------------------------------------

class SubscriptionPlan(models.Model):
    """
    A product tier available on the Cytova platform.
    Lives in the public schema. Managed by platform admins.

    `code` is the stable identifier used in API references and billing
    integrations (e.g. TRIAL, STARTER, PRO, ENTERPRISE).

    `is_trial` marks the plan used for automatic signup trial subscriptions.
    Exactly one active trial plan should exist at any time — the onboarding
    flow resolves it via `SubscriptionPlan.objects.filter(is_trial=True, is_active=True)`.

    `is_public` controls whether the plan is shown on the public pricing page.

    Pricing fields are informational for now — no payment processing yet.
    `features` is a JSON dict for frontend display and future feature gating.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=30, unique=True, db_index=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')

    is_trial = models.BooleanField(
        default=False,
        db_index=True,
        help_text='If True, this plan is used for automatic trial subscriptions on signup.',
    )
    is_public = models.BooleanField(
        default=True,
        help_text='If True, this plan is visible on the public pricing page.',
    )
    trial_duration_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Trial duration in days. Required when is_trial=True. Null for paid plans.',
    )

    monthly_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Monthly price in base currency. Informational until billing is implemented.',
    )
    yearly_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Yearly price in base currency. Informational until billing is implemented.',
    )
    features = models.JSONField(
        default=dict,
        blank=True,
        help_text='Feature flags / limits for this plan (e.g. max_users, max_exams).',
    )
    display_order = models.IntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Subscription Plan'
        verbose_name_plural = 'Subscription Plans'
        ordering = ['display_order', 'name']

    def __str__(self):
        return f'{self.name} ({self.code})'


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

class SubscriptionStatus(models.TextChoices):
    TRIAL     = 'TRIAL',     'Trial'
    ACTIVE    = 'ACTIVE',    'Active'
    EXPIRED   = 'EXPIRED',   'Expired'
    SUSPENDED = 'SUSPENDED', 'Suspended'
    CANCELLED = 'CANCELLED', 'Cancelled'


# Terminal statuses — no further transitions
TERMINAL_SUBSCRIPTION_STATUSES = frozenset({
    SubscriptionStatus.CANCELLED,
})


class Subscription(models.Model):
    """
    Links a Tenant to a SubscriptionPlan with lifecycle tracking.
    Lives in the public schema. One active subscription per tenant at a time.

    Lifecycle:
        TRIAL  → ACTIVE (on activation / payment)
        TRIAL  → EXPIRED (trial_end_date passed)
        ACTIVE → SUSPENDED (non-payment / admin action)
        ACTIVE → EXPIRED (end_date passed)
        ACTIVE → CANCELLED (lab requests cancellation)
        SUSPENDED → ACTIVE (reactivation)
        SUSPENDED → CANCELLED (admin closes)
        EXPIRED → ACTIVE (renewal)

    `started_at` and `current_period_end` define the billing cycle.
    `trial_end_date` is set only for TRIAL subscriptions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='subscriptions',
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name='subscriptions',
    )
    status = models.CharField(
        max_length=15,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.TRIAL,
        db_index=True,
    )

    started_at = models.DateTimeField(
        default=timezone.now,
        help_text='When this subscription began.',
    )
    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text='End of the current billing period. Null for open-ended.',
    )
    trial_end_date = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='When the trial expires. Null if not a trial.',
    )

    activated_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the subscription transitioned from TRIAL to ACTIVE.',
    )
    suspended_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Who cancelled: "admin", "platform", or "system".',
    )

    notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Subscription'
        verbose_name_plural = 'Subscriptions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['status', 'trial_end_date']),
        ]

    def __str__(self):
        return f'{self.tenant.subdomain} — {self.plan.code} [{self.status}]'

    @property
    def is_usable(self):
        """True if the tenant should have access to the application."""
        return self.status in (SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE)

    @property
    def trial_days_remaining(self):
        if self.status != SubscriptionStatus.TRIAL or not self.trial_end_date:
            return None
        remaining = (self.trial_end_date - timezone.now()).days
        return max(0, remaining)


# ---------------------------------------------------------------------------
# Legacy compatibility — keep Plan TextChoices for Tenant.plan field
# ---------------------------------------------------------------------------

class Plan(models.TextChoices):
    FREE = 'FREE', 'Free'
    STARTER = 'STARTER', 'Starter'
    PRO = 'PRO', 'Pro'
    ENTERPRISE = 'ENTERPRISE', 'Enterprise'


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------

class TenantCodeCounter(models.Model):
    """
    Single-row counter in the public schema used to allocate the next
    tenant numeric code atomically via SELECT ... FOR UPDATE.

    Kept as an explicit DB row (rather than a Python-side constant or a
    ``MAX(code)+1`` query) so concurrent onboarding transactions serialise
    cleanly on the row lock. Starting value is 0 → first allocated code
    is "0001".
    """
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Tenant Code Counter'
        verbose_name_plural = 'Tenant Code Counter'

    def __str__(self):
        return f'TenantCodeCounter(last_value={self.last_value})'


class Tenant(TenantMixin):
    """
    Represents a medical laboratory on the Cytova platform.

    Lives in the public schema. Each Tenant record corresponds to one
    isolated PostgreSQL schema (e.g. schema_laba). django-tenants creates
    and migrates that schema automatically via auto_create_schema = True.

    The schema_name field is provided by TenantMixin and must be set
    explicitly at provisioning time (convention: 'schema_' + subdomain).

    `plan` is a legacy convenience field kept for backward compatibility.
    The authoritative subscription state is in the Subscription model.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=100, unique=True)
    numeric_code = models.CharField(
        max_length=4,
        unique=True,
        editable=False,
        help_text='Stable 4-digit zero-padded tenant identifier used as the '
                  'prefix of generated label codes. Immutable once assigned.',
    )
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.STARTER)

    # Geography — collected at onboarding. Optional at the model level so legacy
    # tenants created before this field existed remain valid; the public signup
    # serializer enforces them as required for new laboratories.
    country = models.CharField(
        max_length=2,
        blank=True,
        default='',
        help_text='ISO 3166-1 alpha-2 country code (e.g. "FR", "US").',
    )
    city = models.CharField(
        max_length=120,
        blank=True,
        default='',
        help_text='Primary city of the laboratory.',
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)

    auto_create_schema = True

    class Meta:
        verbose_name = 'Tenant'
        verbose_name_plural = 'Tenants'

    def save(self, *args, **kwargs):
        # Auto-allocate the numeric code on first save so callers (onboarding
        # flow, management shell, tests) never have to supply it manually.
        # Once allocated, the value is immutable — subsequent saves never
        # overwrite it because the ``not self.numeric_code`` guard is only
        # truthy on insert.
        if not self.numeric_code:
            from .services import TenantCodeAllocator
            self.numeric_code = TenantCodeAllocator.allocate()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.subdomain})'

    @property
    def is_suspended(self):
        return self.suspended_at is not None and not self.is_active

    @property
    def active_subscription(self):
        """
        Returns the current usable subscription, or None.

        Performance: the SubscriptionEnforcementMiddleware caches the result
        on the tenant instance (_cached_active_subscription) so downstream
        code that accesses this property within the same request avoids a
        repeat DB query.
        """
        cached = getattr(self, '_cached_active_subscription', None)
        if cached is not None:
            return cached
        # Sentinel to distinguish "cached None" (no subscription) from "not cached"
        if hasattr(self, '_cached_active_subscription'):
            return None
        result = (
            self.subscriptions
            .filter(status__in=[SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE])
            .select_related('plan')
            .first()
        )
        self._cached_active_subscription = result
        return result


class Domain(DomainMixin):
    """
    Maps a fully-qualified domain (or subdomain) to a Tenant.

    Example:
        Domain(domain='laba.cytova.io', tenant=<Tenant laba>, is_primary=True)

    In development, use entries in /etc/hosts and domains like 'laba.localhost'.
    """

    class Meta:
        verbose_name = 'Domain'
        verbose_name_plural = 'Domains'

    def __str__(self):
        return self.domain


class PlatformRole(models.TextChoices):
    PLATFORM_OWNER = 'PLATFORM_OWNER', 'Platform Owner'
    PLATFORM_STAFF = 'PLATFORM_STAFF', 'Platform Staff'


class PlatformAdminManager(BaseUserManager):
    def create(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email address is required.')
        admin = self.model(email=self.normalize_email(email), **extra_fields)
        admin.set_password(password)
        admin.save(using=self._db)
        return admin


class PlatformAdmin(AbstractBaseUser):
    """
    Platform-level administrator. Lives in the public schema (apps.tenants is SHARED).

    Completely separate from per-tenant StaffUser. Used to manage tenant provisioning
    via the admin.cytova.io API. Does NOT use AUTH_USER_MODEL — has its own
    PlatformAdminJWTAuthentication class.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    role = models.CharField(
        max_length=20,
        choices=PlatformRole.choices,
        default=PlatformRole.PLATFORM_STAFF,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = PlatformAdminManager()

    class Meta:
        verbose_name = 'Platform Admin'
        verbose_name_plural = 'Platform Admins'

    def __str__(self):
        return self.email

    @property
    def is_platform_owner(self):
        return self.role == PlatformRole.PLATFORM_OWNER


# ---------------------------------------------------------------------------
# Onboarding Registration (pre-tenant)
# ---------------------------------------------------------------------------

class OnboardingStatus(models.TextChoices):
    PENDING_EMAIL = 'PENDING_EMAIL',  'Pending email verification'
    EMAIL_VERIFIED = 'EMAIL_VERIFIED', 'Email verified'
    COMPLETED = 'COMPLETED',     'Completed'
    EXPIRED = 'EXPIRED',       'Expired'


# Statuses past which no further state transitions are permitted.
TERMINAL_ONBOARDING_STATUSES = frozenset({
    OnboardingStatus.COMPLETED,
    OnboardingStatus.EXPIRED,
})


class OnboardingRegistration(models.Model):
    """
    Pre-tenant onboarding state. Lives in the public schema.

    Created when a user starts signup. The associated Tenant + StaffUser
    + Subscription are created **only** at completion time, after email
    verification has succeeded. This avoids orphan schemas from abandoned
    signups and lets the platform garbage-collect stale registrations
    without touching tenant data.

    Sensitive data:
      - The verification code is stored only as an HMAC-SHA256 hash,
        never in plaintext (see ``OnboardingService._hash_code``).
      - ``failed_attempts`` + ``locked_until`` provide brute-force defence
        even if the row leaks.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Admin identity (collected on Step 1)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=30, blank=True, default='')

    # Email verification state
    verification_code_hash = models.CharField(max_length=128, blank=True, default='')
    code_expires_at = models.DateTimeField(null=True, blank=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_code_sent_at = models.DateTimeField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    # Lifecycle
    status = models.CharField(
        max_length=20,
        choices=OnboardingStatus.choices,
        default=OnboardingStatus.PENDING_EMAIL,
        db_index=True,
    )
    tenant = models.OneToOneField(
        'tenants.Tenant',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='onboarding_registration',
        help_text='Set at completion time — points to the tenant that was created from this registration.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Onboarding Registration'
        verbose_name_plural = 'Onboarding Registrations'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email', 'status']),
        ]

    def __str__(self):
        return f'{self.email} [{self.status}]'

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())

    @property
    def is_code_expired(self) -> bool:
        return bool(self.code_expires_at and self.code_expires_at <= timezone.now())

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ONBOARDING_STATUSES


# Import PlatformAuditLog so Django discovers it for migrations
from .platform_audit import PlatformAuditLog, PlatformAction  # noqa: E402, F401

__all__ = [
    'SubscriptionPlan', 'SubscriptionStatus', 'Subscription',
    'Plan', 'Tenant', 'TenantCodeCounter', 'Domain',
    'PlatformRole', 'PlatformAdmin', 'PlatformAdminManager',
    'PlatformAuditLog', 'PlatformAction',
    'OnboardingRegistration', 'OnboardingStatus', 'TERMINAL_ONBOARDING_STATUSES',
]
