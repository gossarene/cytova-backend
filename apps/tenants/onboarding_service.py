"""
Cytova — Laboratory Onboarding Service

Implements the multi-step onboarding flow:

    1. start()         — create OnboardingRegistration, send verification code
    2. verify_email()  — validate the 6-digit code → mark EMAIL_VERIFIED
    3. resend_code()   — resend the code (with cooldown + lockout protection)
    4. complete()      — once email is verified and lab info is provided,
                         provision the Tenant + Domain + trial Subscription
                         + initial LAB_ADMIN StaffUser

Tenants are NEVER created before email verification has succeeded — this is
the central invariant that prevents orphan schemas from abandoned signups.

The tenant.save() call still triggers DDL (CREATE SCHEMA) which auto-commits
in PostgreSQL and cannot be rolled back. Post-DDL operations run in a regular
transaction within the new schema. If they fail, the tenant + schema exist
but are empty; the registration stays at EMAIL_VERIFIED so a cleanup task
can garbage-collect tenants without admin users.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.tenants.models import (
    Domain, OnboardingRegistration, OnboardingStatus, Plan, Tenant,
)
from common.email import get_email_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OnboardingError(Exception):
    """Base class for service-level onboarding errors. Translated to HTTP
    responses by the views layer."""
    code = 'ONBOARDING_ERROR'
    message = 'Onboarding failed.'


class OnboardingNotFound(OnboardingError):
    code = 'ONBOARDING_NOT_FOUND'
    message = 'Onboarding session not found.'


class OnboardingNotReady(OnboardingError):
    code = 'ONBOARDING_NOT_READY'
    message = 'Email must be verified before completing onboarding.'


class InvalidVerificationCode(OnboardingError):
    code = 'INVALID_CODE'
    message = 'Invalid verification code.'

    def __init__(self, attempts_remaining: int):
        self.attempts_remaining = attempts_remaining
        super().__init__(self.message)


class VerificationCodeExpired(OnboardingError):
    code = 'CODE_EXPIRED'
    message = 'Verification code has expired. Request a new one.'


class VerificationLocked(OnboardingError):
    code = 'TOO_MANY_ATTEMPTS'
    message = 'Too many incorrect attempts. Try again later.'

    def __init__(self, locked_until):
        self.locked_until = locked_until
        super().__init__(self.message)


class ResendCooldown(OnboardingError):
    code = 'RESEND_COOLDOWN'
    message = 'Please wait before requesting another code.'

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(self.message)


class EmailDeliveryError(OnboardingError):
    """The email provider rejected or failed to deliver the verification
    code. Distinct from configuration errors (which raise on service
    construction) — this represents a transient delivery issue at runtime."""
    code = 'EMAIL_DELIVERY_FAILED'
    message = 'Could not send verification email. Please try again.'


# ---------------------------------------------------------------------------
# Result value object
# ---------------------------------------------------------------------------

class OnboardingResult:
    """Returned by OnboardingService.complete()."""
    __slots__ = ('tenant', 'domain', 'admin_user', 'subscription', 'registration')

    def __init__(self, tenant, domain, admin_user, subscription, registration):
        self.tenant = tenant
        self.domain = domain
        self.admin_user = admin_user
        self.subscription = subscription
        self.registration = registration


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class OnboardingService:

    CODE_TTL_MINUTES = 10
    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_MINUTES = 15
    RESEND_COOLDOWN_SECONDS = 60
    REGISTRATION_TTL_HOURS = 24

    # ----- Public API --------------------------------------------------

    @staticmethod
    def start(*, first_name: str, last_name: str, email: str, phone: str) -> OnboardingRegistration:
        """
        Begin onboarding. Idempotent on email — if a non-terminal record
        exists for this email, it is reused (resume flow). Always issues a
        fresh code unless email is already verified, so resend-via-restart
        works without a special endpoint.

        Returns the registration regardless of whether it was newly created
        or reused; callers must NOT use the return value to detect existence
        (this would leak email enumeration to attackers).
        """
        email = email.strip().lower()
        now = timezone.now()

        # Lazy garbage collection — mark very old pending records as expired
        OnboardingRegistration.objects.filter(
            email=email,
            status=OnboardingStatus.PENDING_EMAIL,
            created_at__lt=now - timedelta(hours=OnboardingService.REGISTRATION_TTL_HOURS),
        ).update(status=OnboardingStatus.EXPIRED, updated_at=now)

        registration = OnboardingRegistration.objects.filter(
            email=email,
            status__in=[OnboardingStatus.PENDING_EMAIL, OnboardingStatus.EMAIL_VERIFIED],
        ).order_by('-created_at').first()

        if registration is None:
            registration = OnboardingRegistration(email=email)

        # Update identity from this latest call (corrects typos, refreshes phone).
        registration.first_name = first_name.strip()
        registration.last_name = last_name.strip()
        registration.phone = (phone or '').strip()

        if registration.status == OnboardingStatus.EMAIL_VERIFIED:
            registration.save()
            return registration

        # New or pending → issue a fresh code (resets failure counters).
        code = OnboardingService._issue_code(registration)
        registration.save()
        OnboardingService._send_verification_email(registration, code)
        return registration

    @staticmethod
    def verify_email(*, registration_id, code: str) -> OnboardingRegistration:
        registration = OnboardingService._get_active(registration_id)

        if registration.status == OnboardingStatus.EMAIL_VERIFIED:
            return registration  # idempotent

        if registration.is_locked:
            raise VerificationLocked(registration.locked_until)

        if not registration.verification_code_hash or registration.is_code_expired:
            raise VerificationCodeExpired()

        if not OnboardingService._hash_eq(code, registration.verification_code_hash):
            registration.failed_attempts += 1
            attempts_remaining = max(0, OnboardingService.MAX_FAILED_ATTEMPTS - registration.failed_attempts)
            if registration.failed_attempts >= OnboardingService.MAX_FAILED_ATTEMPTS:
                registration.locked_until = timezone.now() + timedelta(
                    minutes=OnboardingService.LOCKOUT_MINUTES,
                )
                # Invalidate the code so the same attacker can't resume even
                # after the lockout — they have to request a new code.
                registration.verification_code_hash = ''
                registration.code_expires_at = None
            registration.save()
            if registration.is_locked:
                raise VerificationLocked(registration.locked_until)
            raise InvalidVerificationCode(attempts_remaining)

        registration.email_verified_at = timezone.now()
        registration.status = OnboardingStatus.EMAIL_VERIFIED
        registration.verification_code_hash = ''
        registration.code_expires_at = None
        registration.failed_attempts = 0
        registration.locked_until = None
        registration.save()

        logger.info('Onboarding email verified: id=%s email=%s', registration.id, registration.email)
        return registration

    @staticmethod
    def resend_code(*, registration_id) -> OnboardingRegistration:
        registration = OnboardingService._get_active(registration_id)

        if registration.status == OnboardingStatus.EMAIL_VERIFIED:
            # Nothing to resend — the user already verified.
            return registration

        if registration.is_locked:
            raise VerificationLocked(registration.locked_until)

        if registration.last_code_sent_at:
            elapsed = (timezone.now() - registration.last_code_sent_at).total_seconds()
            if elapsed < OnboardingService.RESEND_COOLDOWN_SECONDS:
                raise ResendCooldown(int(OnboardingService.RESEND_COOLDOWN_SECONDS - elapsed) + 1)

        code = OnboardingService._issue_code(registration)
        registration.save()
        OnboardingService._send_verification_email(registration, code)
        return registration

    @staticmethod
    def complete(
        *,
        registration_id,
        laboratory_name: str,
        country: str,
        city: str,
        slug: str,
        password: str,
    ) -> OnboardingResult:
        registration = OnboardingService._get_active(registration_id)

        if registration.status != OnboardingStatus.EMAIL_VERIFIED:
            raise OnboardingNotReady()

        result = OnboardingService._provision_tenant(
            registration=registration,
            laboratory_name=laboratory_name,
            country=country,
            city=city,
            slug=slug,
            password=password,
        )

        registration.tenant = result.tenant
        registration.status = OnboardingStatus.COMPLETED
        registration.save(update_fields=['tenant', 'status', 'updated_at'])

        return result

    # ----- Tenant provisioning (private) ------------------------------

    @staticmethod
    def _provision_tenant(
        *,
        registration: OnboardingRegistration,
        laboratory_name: str,
        country: str,
        city: str,
        slug: str,
        password: str,
    ) -> OnboardingResult:
        from .subscription_service import SubscriptionService

        schema_name = f'schema_{slug}'

        # Resolve trial plan BEFORE creating anything — fail fast if missing.
        trial_plan = SubscriptionService.get_default_trial_plan()

        # ---- Tenant + Domain in public schema ----
        tenant = Tenant(
            schema_name=schema_name,
            name=laboratory_name,
            subdomain=slug,
            country=country,
            city=city,
            plan=Plan.STARTER,
            is_active=True,
            activated_at=timezone.now(),
        )
        tenant.save()  # DDL: CREATE SCHEMA + run migrations

        primary_domain_name = f'{slug}.{settings.CYTOVA_DOMAIN}'
        Domain.objects.create(
            domain=primary_domain_name,
            tenant=tenant,
            is_primary=True,
        )

        # ---- Trial subscription ----
        subscription = SubscriptionService.create_trial(
            tenant=tenant,
            plan=trial_plan,
        )

        # ---- Admin user inside the tenant schema ----
        with schema_context(schema_name):
            from apps.users.models import StaffUser, Role

            admin_user = StaffUser.objects.create_user(
                email=registration.email,
                password=password,
                first_name=registration.first_name,
                last_name=registration.last_name,
                phone=registration.phone,
                role=Role.LAB_ADMIN,
                is_staff=True,
                is_superuser=True,
            )

            OnboardingService._seed_defaults()

            OnboardingService._audit_onboarding(tenant, admin_user, subscription)

        logger.info(
            'Laboratory onboarded: name=%s slug=%s admin=%s plan=%s trial_days=%d',
            laboratory_name, slug, admin_user.email,
            trial_plan.code, trial_plan.trial_duration_days,
        )

        return OnboardingResult(
            tenant=tenant,
            domain=primary_domain_name,
            admin_user=admin_user,
            subscription=subscription,
            registration=registration,
        )

    # ----- Verification code helpers ----------------------------------

    @staticmethod
    def _issue_code(registration: OnboardingRegistration) -> str:
        """Mutate the registration to embed a fresh code; return the plaintext
        code (caller is responsible for delivery — never persisted)."""
        code = f'{secrets.randbelow(10 ** 6):06d}'
        registration.verification_code_hash = OnboardingService._hash_code(code)
        registration.code_expires_at = (
            timezone.now() + timedelta(minutes=OnboardingService.CODE_TTL_MINUTES)
        )
        registration.last_code_sent_at = timezone.now()
        # Issuing a new code resets failure state; otherwise an attacker
        # could keep a record permanently locked by triggering wrong codes.
        registration.failed_attempts = 0
        registration.locked_until = None
        return code

    @staticmethod
    def _hash_code(code: str) -> str:
        """HMAC-SHA256 of the code under SECRET_KEY. Hash-only storage prevents
        DB-leak replay; HMAC keying makes offline rainbow-table brute force on
        the small 1M code space useless without the server secret."""
        return hmac.new(
            settings.SECRET_KEY.encode(),
            code.strip().encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _hash_eq(plain_code: str, hashed: str) -> bool:
        return hmac.compare_digest(OnboardingService._hash_code(plain_code), hashed)

    # ----- Email delivery ---------------------------------------------

    @staticmethod
    def _send_verification_email(registration: OnboardingRegistration, code: str) -> None:
        """Hand off the rendered verification message to the configured
        EmailProvider. Domain code never speaks directly to a transport —
        provider selection lives entirely in `common.email`.

        The provider catches its own delivery errors and reports them via
        the EmailResult contract; we translate a failed delivery into a
        controlled EmailDeliveryError so the view returns a clean error
        envelope instead of a 201 with no email."""
        result = get_email_service().send_verification_code(
            recipient_email=registration.email,
            recipient_name=registration.first_name,
            code=code,
            expires_minutes=OnboardingService.CODE_TTL_MINUTES,
        )
        if not result.ok:
            # Provider already logged structured detail (status, recipient
            # domain, error type). Don't re-log the code here under any
            # circumstance.
            raise EmailDeliveryError()

    # ----- Lookup helper ----------------------------------------------

    @staticmethod
    def _get_active(registration_id) -> OnboardingRegistration:
        try:
            registration = OnboardingRegistration.objects.get(pk=registration_id)
        except (OnboardingRegistration.DoesNotExist, ValueError):
            # ValueError covers malformed UUID strings.
            raise OnboardingNotFound() from None
        if registration.is_terminal:
            raise OnboardingNotFound()
        return registration

    # ----- Default tenant data ----------------------------------------

    @staticmethod
    def _seed_defaults():
        """Create default catalog families and stock categories in the new
        tenant schema. Called inside schema_context."""
        from apps.catalog.models import ExamCategory, ExamFamily

        default_families = [
            ('Hematology', 1),
            ('Biochemistry', 2),
            ('Microbiology', 3),
            ('Immunology', 4),
            ('Parasitology', 5),
        ]
        for name, order in default_families:
            ExamFamily.objects.get_or_create(name=name, defaults={'display_order': order})
            ExamCategory.objects.get_or_create(name=name, defaults={'display_order': order})

        from apps.stock.models import StockCategory

        default_stock_categories = [
            ('Reagents', 1),
            ('Consumables', 2),
            ('Equipment', 3),
        ]
        for name, order in default_stock_categories:
            StockCategory.objects.get_or_create(name=name, defaults={'display_order': order})

    @staticmethod
    def _audit_onboarding(tenant, admin_user, subscription):
        from apps.audit.models import AuditLog, AuditAction, ActorType

        AuditLog.objects.create(
            actor_type=ActorType.SYSTEM,
            actor_id=admin_user.id,
            actor_email=admin_user.email,
            action=AuditAction.CREATE,
            entity_type='TenantOnboarding',
            entity_id=tenant.id,
            diff={
                'tenant_name': tenant.name,
                'subdomain': tenant.subdomain,
                'admin_email': admin_user.email,
                'plan_code': subscription.plan.code,
                'subscription_status': subscription.status,
                'trial_end_date': str(subscription.trial_end_date),
                'trial_duration_days': subscription.plan.trial_duration_days,
            },
        )
