"""
Tests for the laboratory self-service onboarding flow.

Uses transactional_db because tenant creation runs DDL. Each test uses
unique slugs/emails via _signup_data() to avoid cross-test collisions
(DDL is not rolled back between tests).
"""
import pytest
from datetime import timedelta

from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework.exceptions import ValidationError

from apps.tenants.models import (
    Domain,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Tenant,
)
from apps.tenants.onboarding_serializers import LaboratorySignupSerializer
from apps.tenants.onboarding_service import OnboardingService


@pytest.fixture(autouse=True)
def _in_tenant_schema():
    yield


@pytest.fixture()
def trial_plan():
    return SubscriptionPlan.objects.get_or_create(
        code='TRIAL',
        defaults={
            'name': 'Free Trial',
            'is_trial': True,
            'is_public': False,
            'trial_duration_days': 14,
        },
    )[0]


_counter = 0


def _signup_data(**overrides):
    global _counter
    _counter += 1
    base = {
        'laboratory_name': f'Lab {_counter}',
        'slug': f'lab-{_counter:04d}',
        'admin_email': f'admin-{_counter}@lab.com',
        'admin_first_name': 'Admin',
        'admin_last_name': 'User',
        'admin_password': 'Str0ng!Pass#2026',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Serializer validation
# ---------------------------------------------------------------------------

class TestSignupSerializer:

    @pytest.mark.django_db(transaction=True)
    def test_valid_full_payload(self):
        data = _signup_data()
        s = LaboratorySignupSerializer(data=data)
        assert s.is_valid(), s.errors

    @pytest.mark.django_db(transaction=True)
    def test_auto_slug_from_name(self):
        s = LaboratorySignupSerializer(data=_signup_data(
            laboratory_name='Hôpital Saint-Luc',
        ))
        s.fields.pop('slug', None)  # let it auto-generate
        # Re-create without slug
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'Hôpital Saint-Luc',
            'admin_email': _signup_data()['admin_email'],
            'admin_first_name': 'Bob',
            'admin_last_name': 'Dupont',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['slug'] == 'hopital-saint-luc'

    @pytest.mark.django_db(transaction=True)
    def test_reserved_slug_rejected(self):
        s = LaboratorySignupSerializer(data=_signup_data(slug='admin'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_weak_password_rejected(self):
        s = LaboratorySignupSerializer(data=_signup_data(admin_password='123'))
        assert not s.is_valid()
        assert 'admin_password' in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_missing_required_fields(self):
        s = LaboratorySignupSerializer(data={})
        assert not s.is_valid()
        for field in ('laboratory_name', 'admin_email', 'admin_first_name',
                      'admin_last_name', 'admin_password'):
            assert field in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_duplicate_slug_rejected(self):
        t = Tenant(name='Existing', subdomain='dup-slug-test', schema_name='schema_dup_slug_test')
        t.save()
        Domain.objects.create(domain='dup-slug-test.cytova.io', tenant=t, is_primary=True)

        s = LaboratorySignupSerializer(data=_signup_data(slug='dup-slug-test'))
        assert not s.is_valid()
        assert 'slug' in s.errors


# ---------------------------------------------------------------------------
# Full onboarding flow
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestOnboardingService:

    def test_signup_creates_tenant_and_admin(self, trial_plan):
        data = _signup_data()
        result = OnboardingService.signup(data)

        assert result.tenant.pk is not None
        assert result.tenant.subdomain == data['slug']
        assert result.tenant.is_active is True
        assert result.domain == f'{data["slug"]}.cytova.io'

        with schema_context(f'schema_{data["slug"]}'):
            from apps.users.models import StaffUser, Role
            admin = StaffUser.objects.get(email=data['admin_email'])
            assert admin.role == Role.LAB_ADMIN
            assert admin.check_password(data['admin_password'])

    def test_signup_creates_trial_subscription(self, trial_plan):
        result = OnboardingService.signup(_signup_data())

        assert result.subscription is not None
        assert result.subscription.status == SubscriptionStatus.TRIAL
        assert result.subscription.plan == trial_plan
        assert result.subscription.plan.code == 'TRIAL'
        assert result.subscription.plan.is_trial is True
        assert result.subscription.trial_end_date is not None
        assert result.subscription.is_usable is True

    def test_trial_end_date_from_plan(self, trial_plan):
        before = timezone.now()
        result = OnboardingService.signup(_signup_data())
        after = timezone.now()

        expected_min = before + timedelta(days=trial_plan.trial_duration_days)
        expected_max = after + timedelta(days=trial_plan.trial_duration_days)
        assert expected_min <= result.subscription.trial_end_date <= expected_max

    def test_trial_duration_respects_plan_config(self):
        """A 30-day trial plan → onboarding creates a 30-day trial."""
        SubscriptionPlan.objects.update_or_create(
            code='TRIAL',
            defaults={
                'name': 'Long Trial', 'is_trial': True,
                'trial_duration_days': 30,
            },
        )
        result = OnboardingService.signup(_signup_data())

        days = result.subscription.trial_days_remaining
        assert days is not None
        assert 29 <= days <= 30

    def test_signup_seeds_default_categories(self, trial_plan):
        data = _signup_data()
        OnboardingService.signup(data)

        with schema_context(f'schema_{data["slug"]}'):
            from apps.catalog.models import ExamCategory
            from apps.stock.models import StockCategory
            assert ExamCategory.objects.filter(name='Hematology').exists()
            assert StockCategory.objects.filter(name='Reagents').exists()

    def test_signup_writes_audit_with_subscription_info(self, trial_plan):
        data = _signup_data()
        result = OnboardingService.signup(data)

        with schema_context(f'schema_{data["slug"]}'):
            from apps.audit.models import AuditLog
            log = AuditLog.objects.filter(
                entity_type='TenantOnboarding',
                entity_id=result.tenant.id,
            ).first()
            assert log is not None
            assert log.diff['plan_code'] == 'TRIAL'
            assert log.diff['subscription_status'] == 'TRIAL'
            assert log.diff['trial_duration_days'] == trial_plan.trial_duration_days

    def test_tenant_isolation_after_signup(self, trial_plan):
        d1 = _signup_data()
        d2 = _signup_data()
        r1 = OnboardingService.signup(d1)
        r2 = OnboardingService.signup(d2)

        with schema_context(f'schema_{d1["slug"]}'):
            from apps.users.models import StaffUser
            assert StaffUser.objects.count() == 1
            assert StaffUser.objects.first().email == d1['admin_email']

        with schema_context(f'schema_{d2["slug"]}'):
            from apps.users.models import StaffUser
            assert StaffUser.objects.count() == 1
            assert StaffUser.objects.first().email == d2['admin_email']

        assert Subscription.objects.filter(tenant=r1.tenant).count() == 1
        assert Subscription.objects.filter(tenant=r2.tenant).count() == 1


# ---------------------------------------------------------------------------
# Error / edge cases
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestOnboardingFailures:

    def test_signup_fails_without_trial_plan(self):
        data = _signup_data()
        with pytest.raises(ValidationError, match='No active trial plan'):
            OnboardingService.signup(data)
        assert not Tenant.objects.filter(subdomain=data['slug']).exists()

    def test_duplicate_slug_blocked_by_serializer(self, trial_plan):
        data = _signup_data()
        OnboardingService.signup(data)

        s = LaboratorySignupSerializer(data=data)
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_no_duplicate_subscription_on_separate_signups(self, trial_plan):
        d1 = _signup_data()
        d2 = _signup_data()
        r1 = OnboardingService.signup(d1)
        r2 = OnboardingService.signup(d2)

        assert Subscription.objects.filter(tenant=r1.tenant).count() == 1
        assert Subscription.objects.filter(tenant=r2.tenant).count() == 1


# ---------------------------------------------------------------------------
# Bootstrap / seed_plans command
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSeedPlansCommand:

    def test_seed_plans_creates_trial_plan(self):
        from django.core.management import call_command
        call_command('seed_plans', verbosity=0)

        trial = SubscriptionPlan.objects.filter(code='TRIAL', is_trial=True).first()
        assert trial is not None
        assert trial.trial_duration_days == 14
        assert trial.is_active is True
        assert trial.is_public is False

    def test_seed_plans_idempotent(self):
        from django.core.management import call_command
        call_command('seed_plans', verbosity=0)
        call_command('seed_plans', verbosity=0)

        assert SubscriptionPlan.objects.filter(code='TRIAL').count() == 1

    def test_seed_plans_creates_all_plans(self):
        from django.core.management import call_command
        call_command('seed_plans', verbosity=0)

        codes = set(SubscriptionPlan.objects.values_list('code', flat=True))
        assert {'TRIAL', 'STARTER', 'PRO', 'ENTERPRISE'}.issubset(codes)

    def test_signup_works_after_seed(self):
        from django.core.management import call_command
        call_command('seed_plans', verbosity=0)

        data = _signup_data()
        result = OnboardingService.signup(data)

        assert result.subscription.status == SubscriptionStatus.TRIAL
        assert result.subscription.plan.code == 'TRIAL'
        assert result.subscription.plan.is_trial is True
