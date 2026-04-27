"""
Tests for the multi-step onboarding flow.

Covers:
  - start() never creates a tenant
  - start() resumes existing pending registrations (idempotent on email)
  - verify_email() success / wrong code / expired code / lockout
  - resend_code() cooldown
  - complete() refuses if email is not verified
  - complete() creates tenant + admin + trial subscription
  - email enumeration defence (start always returns same shape)
  - the issued verification code is never persisted as plaintext
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.tenants.models import (
    Domain, OnboardingRegistration, OnboardingStatus, SubscriptionPlan, Tenant,
)
from apps.tenants.onboarding_service import (
    InvalidVerificationCode,
    OnboardingNotFound,
    OnboardingNotReady,
    OnboardingService,
    ResendCooldown,
    VerificationCodeExpired,
    VerificationLocked,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def trial_plan(db):
    return SubscriptionPlan.objects.get_or_create(
        code='TRIAL_DEFAULT',
        defaults={
            'name': 'Trial',
            'is_trial': True,
            'is_active': True,
            'trial_duration_days': 14,
        },
    )[0]


@pytest.fixture
def captured_codes(monkeypatch):
    """Patch the email sender to keep the issued code in a list rather than
    actually sending mail. Returns the list — callers grab `codes[-1]` to
    obtain the most recently issued plaintext code."""
    codes: list[str] = []

    def _capture(_registration, code):
        codes.append(code)

    monkeypatch.setattr(
        OnboardingService,
        '_send_verification_email',
        staticmethod(_capture),
    )
    return codes


_counter = 0


def _identity(**overrides):
    """Generate unique identity payloads so concurrent tests don't collide."""
    global _counter
    _counter += 1
    base = {
        'first_name': 'Alice',
        'last_name': 'Dupont',
        'email': f'admin-{_counter}@example.com',
        'phone': '+33 1 00 00 00 00',
    }
    base.update(overrides)
    return base


def _lab_payload(slug):
    return {
        'laboratory_name': f'Lab {slug}',
        'country': 'FR',
        'city': 'Paris',
        'slug': slug,
        'password': 'Str0ng!Pass#2026',
    }


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestOnboardingStart:

    def test_creates_pending_registration_without_tenant(self, captured_codes):
        before = Tenant.objects.count()
        registration = OnboardingService.start(**_identity())
        assert registration.id is not None
        assert registration.status == OnboardingStatus.PENDING_EMAIL
        assert registration.tenant is None
        assert Tenant.objects.count() == before  # zero tenants created
        assert len(captured_codes) == 1

    def test_persists_only_hashed_code_never_plaintext(self, captured_codes):
        registration = OnboardingService.start(**_identity())
        plaintext = captured_codes[-1]
        assert plaintext.isdigit() and len(plaintext) == 6
        assert plaintext not in registration.verification_code_hash
        assert len(registration.verification_code_hash) == 64  # SHA-256 hex

    def test_resumes_existing_pending_registration(self, captured_codes):
        identity = _identity()
        first = OnboardingService.start(**identity)
        second = OnboardingService.start(**identity)
        # Same email, both calls return the same row — no parallel pending duplicates.
        assert first.id == second.id
        assert OnboardingRegistration.objects.filter(
            email=identity['email'],
            status=OnboardingStatus.PENDING_EMAIL,
        ).count() == 1

    def test_resume_picks_up_verified_record_without_resending_code(self, captured_codes):
        identity = _identity()
        first = OnboardingService.start(**identity)
        OnboardingService.verify_email(registration_id=first.id, code=captured_codes[-1])

        codes_before = len(captured_codes)
        again = OnboardingService.start(**identity)

        assert again.id == first.id
        assert again.status == OnboardingStatus.EMAIL_VERIFIED
        assert len(captured_codes) == codes_before  # no new code sent


# ---------------------------------------------------------------------------
# verify_email()
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVerifyEmail:

    def test_correct_code_marks_verified_and_clears_hash(self, captured_codes):
        registration = OnboardingService.start(**_identity())
        OnboardingService.verify_email(
            registration_id=registration.id,
            code=captured_codes[-1],
        )
        registration.refresh_from_db()
        assert registration.status == OnboardingStatus.EMAIL_VERIFIED
        assert registration.email_verified_at is not None
        # Hash and expiration cleared after success.
        assert registration.verification_code_hash == ''
        assert registration.code_expires_at is None
        assert registration.failed_attempts == 0

    def test_wrong_code_increments_failures(self, captured_codes):
        registration = OnboardingService.start(**_identity())
        with pytest.raises(InvalidVerificationCode) as exc_info:
            OnboardingService.verify_email(registration_id=registration.id, code='000000')
        registration.refresh_from_db()
        assert registration.failed_attempts == 1
        assert exc_info.value.attempts_remaining == 4

    def test_lockout_after_max_attempts(self, captured_codes):
        registration = OnboardingService.start(**_identity())
        for _ in range(OnboardingService.MAX_FAILED_ATTEMPTS - 1):
            with pytest.raises(InvalidVerificationCode):
                OnboardingService.verify_email(registration_id=registration.id, code='000000')
        with pytest.raises(VerificationLocked):
            OnboardingService.verify_email(registration_id=registration.id, code='000000')
        registration.refresh_from_db()
        assert registration.is_locked
        assert registration.locked_until is not None
        # Code is invalidated alongside the lockout — even after the lockout
        # window the attacker would need a fresh code.
        assert registration.verification_code_hash == ''

    def test_expired_code_rejected(self, captured_codes):
        registration = OnboardingService.start(**_identity())
        registration.code_expires_at = timezone.now() - timedelta(seconds=1)
        registration.save()
        with pytest.raises(VerificationCodeExpired):
            OnboardingService.verify_email(registration_id=registration.id, code=captured_codes[-1])

    def test_unknown_registration_id_404(self):
        import uuid as _uuid
        with pytest.raises(OnboardingNotFound):
            OnboardingService.verify_email(registration_id=_uuid.uuid4(), code='000000')


# ---------------------------------------------------------------------------
# resend_code()
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestResendCode:

    def test_resend_blocked_during_cooldown(self, captured_codes):
        registration = OnboardingService.start(**_identity())
        # last_code_sent_at is "now" → cooldown active
        with pytest.raises(ResendCooldown) as exc_info:
            OnboardingService.resend_code(registration_id=registration.id)
        assert exc_info.value.retry_after_seconds > 0

    def test_resend_after_cooldown_succeeds(self, captured_codes):
        registration = OnboardingService.start(**_identity())
        codes_before = len(captured_codes)
        registration.last_code_sent_at = timezone.now() - timedelta(seconds=120)
        registration.save()
        OnboardingService.resend_code(registration_id=registration.id)
        assert len(captured_codes) == codes_before + 1


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestComplete:

    def test_refuses_when_email_not_verified(self, trial_plan, captured_codes):
        registration = OnboardingService.start(**_identity())
        before = Tenant.objects.count()
        with pytest.raises(OnboardingNotReady):
            OnboardingService.complete(
                registration_id=registration.id,
                **_lab_payload(f'lab-not-ready-{_counter}'),
            )
        assert Tenant.objects.count() == before
        registration.refresh_from_db()
        assert registration.tenant is None

    def test_creates_tenant_admin_subscription_after_verify(self, trial_plan, captured_codes):
        registration = OnboardingService.start(**_identity())
        OnboardingService.verify_email(registration_id=registration.id, code=captured_codes[-1])

        slug = f'lab-complete-{_counter}'
        result = OnboardingService.complete(
            registration_id=registration.id,
            **_lab_payload(slug),
        )

        # Tenant created with the captured geography
        assert result.tenant.subdomain == slug
        assert result.tenant.country == 'FR'
        assert result.tenant.city == 'Paris'
        assert Domain.objects.filter(tenant=result.tenant, is_primary=True).exists()

        # Trial subscription
        assert result.subscription is not None
        assert result.subscription.plan.is_trial
        assert result.subscription.trial_end_date is not None

        # Admin user created in the tenant schema with identity from the registration
        with schema_context(result.tenant.schema_name):
            from apps.users.models import StaffUser, Role
            user = StaffUser.objects.get(email=registration.email)
            assert user.role == Role.LAB_ADMIN
            assert user.first_name == registration.first_name
            assert user.phone == registration.phone

        registration.refresh_from_db()
        assert registration.status == OnboardingStatus.COMPLETED
        assert registration.tenant_id == result.tenant.id


# ---------------------------------------------------------------------------
# Email enumeration defence
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestEmailEnumeration:

    def test_start_with_completed_email_creates_fresh_pending_record(self, trial_plan, captured_codes):
        identity = _identity()
        first = OnboardingService.start(**identity)
        OnboardingService.verify_email(registration_id=first.id, code=captured_codes[-1])
        OnboardingService.complete(
            registration_id=first.id,
            **_lab_payload(f'lab-enum-{_counter}'),
        )

        second = OnboardingService.start(**identity)
        assert second.id != first.id
        assert second.status == OnboardingStatus.PENDING_EMAIL
        assert second.tenant is None
