"""
Tests for the laboratory self-service onboarding flow.

These tests use transactional_db because tenant creation runs DDL
(CREATE SCHEMA) which auto-commits and cannot be rolled back.
"""
import pytest
from django_tenants.utils import schema_context

from apps.tenants.models import Tenant, Domain
from apps.tenants.onboarding_serializers import (
    RESERVED_SLUGS,
    LaboratorySignupSerializer,
)
from apps.tenants.onboarding_service import OnboardingService


# ---------------------------------------------------------------------------
# Override the autouse _in_tenant_schema for this module — onboarding
# operates in the PUBLIC schema, not inside a tenant.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_tenant_schema():
    """Override: onboarding tests run in the public schema."""
    yield


# ---------------------------------------------------------------------------
# Serializer validation
# ---------------------------------------------------------------------------

class TestSignupSerializer:

    @pytest.mark.django_db(transaction=True)
    def test_valid_full_payload(self):
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'City Lab',
            'slug': 'city-lab',
            'admin_email': 'admin@citylab.com',
            'admin_first_name': 'Alice',
            'admin_last_name': 'Martin',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['slug'] == 'city-lab'

    @pytest.mark.django_db(transaction=True)
    def test_auto_slug_from_name(self):
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'Hôpital Saint-Luc',
            'admin_email': 'admin@saintluc.com',
            'admin_first_name': 'Bob',
            'admin_last_name': 'Dupont',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert s.is_valid(), s.errors
        slug = s.validated_data['slug']
        assert slug == 'hopital-saint-luc'
        assert slug.isascii()

    @pytest.mark.django_db(transaction=True)
    def test_slug_lowercased(self):
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'Test',
            'slug': 'MyLab',
            'admin_email': 'a@b.com',
            'admin_first_name': 'A',
            'admin_last_name': 'B',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['slug'] == 'mylab'

    @pytest.mark.django_db(transaction=True)
    def test_reserved_slug_rejected(self):
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'Admin Lab',
            'slug': 'admin',
            'admin_email': 'a@b.com',
            'admin_first_name': 'A',
            'admin_last_name': 'B',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert not s.is_valid()
        assert 'slug' in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_slug_too_short(self):
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'X',
            'slug': 'ab',
            'admin_email': 'a@b.com',
            'admin_first_name': 'A',
            'admin_last_name': 'B',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert not s.is_valid()
        assert 'slug' in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_slug_invalid_chars(self):
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'Test',
            'slug': 'my_lab!',
            'admin_email': 'a@b.com',
            'admin_first_name': 'A',
            'admin_last_name': 'B',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert not s.is_valid()
        assert 'slug' in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_weak_password_rejected(self):
        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'Test Lab',
            'slug': 'test-lab',
            'admin_email': 'a@b.com',
            'admin_first_name': 'A',
            'admin_last_name': 'B',
            'admin_password': '123',
        })
        assert not s.is_valid()
        assert 'admin_password' in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_missing_required_fields(self):
        s = LaboratorySignupSerializer(data={})
        assert not s.is_valid()
        assert 'laboratory_name' in s.errors
        assert 'admin_email' in s.errors
        assert 'admin_first_name' in s.errors
        assert 'admin_last_name' in s.errors
        assert 'admin_password' in s.errors

    @pytest.mark.django_db(transaction=True)
    def test_duplicate_slug_rejected(self):
        # Pre-create a tenant with this slug
        t = Tenant(name='Existing', subdomain='taken-slug', schema_name='schema_taken_slug')
        t.save()
        Domain.objects.create(domain='taken-slug.cytova.io', tenant=t, is_primary=True)

        s = LaboratorySignupSerializer(data={
            'laboratory_name': 'New Lab',
            'slug': 'taken-slug',
            'admin_email': 'a@b.com',
            'admin_first_name': 'A',
            'admin_last_name': 'B',
            'admin_password': 'Str0ng!Pass#2026',
        })
        assert not s.is_valid()
        assert 'slug' in s.errors


# ---------------------------------------------------------------------------
# Full onboarding flow
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestOnboardingService:

    def test_signup_creates_tenant_and_admin(self):
        result = OnboardingService.signup({
            'laboratory_name': 'Demo Lab',
            'slug': 'demo-lab',
            'admin_email': 'admin@demolab.com',
            'admin_first_name': 'Demo',
            'admin_last_name': 'Admin',
            'admin_password': 'Str0ng!Pass#2026',
        })

        # Tenant created in public schema
        assert result.tenant.pk is not None
        assert result.tenant.subdomain == 'demo-lab'
        assert result.tenant.schema_name == 'schema_demo-lab'
        assert result.tenant.is_active is True
        assert result.tenant.plan == 'STARTER'

        # Domain created
        assert result.domain == 'demo-lab.cytova.io'
        assert Domain.objects.filter(
            tenant=result.tenant, is_primary=True,
        ).exists()

        # Admin user created inside tenant schema
        with schema_context('schema_demo-lab'):
            from apps.users.models import StaffUser, Role

            admin = StaffUser.objects.get(email='admin@demolab.com')
            assert admin.role == Role.LAB_ADMIN
            assert admin.first_name == 'Demo'
            assert admin.is_staff is True
            assert admin.is_superuser is True
            assert admin.check_password('Str0ng!Pass#2026')

    def test_signup_seeds_default_categories(self):
        OnboardingService.signup({
            'laboratory_name': 'Seed Lab',
            'slug': 'seed-lab',
            'admin_email': 'admin@seedlab.com',
            'admin_first_name': 'Seed',
            'admin_last_name': 'Admin',
            'admin_password': 'Str0ng!Pass#2026',
        })

        with schema_context('schema_seed-lab'):
            from apps.catalog.models import ExamCategory
            from apps.stock.models import StockCategory

            assert ExamCategory.objects.filter(name='Hematology').exists()
            assert ExamCategory.objects.filter(name='Biochemistry').exists()
            assert StockCategory.objects.filter(name='Reagents').exists()
            assert StockCategory.objects.filter(name='Consumables').exists()

    def test_signup_writes_audit_log(self):
        result = OnboardingService.signup({
            'laboratory_name': 'Audit Lab',
            'slug': 'audit-lab',
            'admin_email': 'admin@auditlab.com',
            'admin_first_name': 'Audit',
            'admin_last_name': 'Admin',
            'admin_password': 'Str0ng!Pass#2026',
        })

        with schema_context('schema_audit-lab'):
            from apps.audit.models import AuditLog

            log = AuditLog.objects.filter(
                entity_type='TenantOnboarding',
                entity_id=result.tenant.id,
            ).first()
            assert log is not None
            assert log.diff['subdomain'] == 'audit-lab'
            assert log.diff['admin_email'] == 'admin@auditlab.com'

    def test_tenant_isolation_after_signup(self):
        """Two signups produce isolated tenants."""
        r1 = OnboardingService.signup({
            'laboratory_name': 'Lab Alpha',
            'slug': 'lab-alpha',
            'admin_email': 'admin@alpha.com',
            'admin_first_name': 'Alpha',
            'admin_last_name': 'Admin',
            'admin_password': 'Str0ng!Pass#2026',
        })
        r2 = OnboardingService.signup({
            'laboratory_name': 'Lab Beta',
            'slug': 'lab-beta',
            'admin_email': 'admin@beta.com',
            'admin_first_name': 'Beta',
            'admin_last_name': 'Admin',
            'admin_password': 'Str0ng!Pass#2026',
        })

        with schema_context('schema_lab-alpha'):
            from apps.users.models import StaffUser
            assert StaffUser.objects.count() == 1
            assert StaffUser.objects.first().email == 'admin@alpha.com'

        with schema_context('schema_lab-beta'):
            from apps.users.models import StaffUser
            assert StaffUser.objects.count() == 1
            assert StaffUser.objects.first().email == 'admin@beta.com'
