"""
Tests for tenant isolation on partner organizations.

These tests create a second tenant (DDL), so they need transactional_db
to avoid conflicts with the outer test transaction.
"""
import pytest
from django_tenants.utils import schema_context

from apps.partners.models import OrganizationType, PartnerOrganization
from apps.partners.services import PartnerOrganizationService
from apps.tenants.models import Tenant, Domain
from apps.users.models import StaffUser, Role


@pytest.fixture()
def second_tenant():
    """
    Creates a second tenant from the public schema (required by django-tenants).
    """
    with schema_context('public'):
        tenant = Tenant(
            name='Other Lab',
            subdomain='otherlab',
            schema_name='schema_otherlab',
        )
        tenant.save()
        Domain.objects.create(
            domain='otherlab.localhost',
            tenant=tenant,
            is_primary=True,
        )
    yield tenant.schema_name
    try:
        with schema_context('public'):
            tenant.delete(force_drop=True)
    except Exception:
        pass


@pytest.mark.django_db(transaction=True)
class TestPartnerTenantIsolation:
    """
    These tests override the default db fixture with transaction=True
    because creating a second tenant requires DDL.
    """

    # Override autouse _in_tenant_schema for this class — we manage schemas manually
    @pytest.fixture(autouse=True)
    def _in_tenant_schema(self, _test_tenant_schema):
        """Override: enter the primary tenant schema without the db fixture."""
        with schema_context(_test_tenant_schema):
            yield

    _admin_counter = 0

    def _create_admin(self):
        TestPartnerTenantIsolation._admin_counter += 1
        return StaffUser.objects.create_user(
            email=f'admin{self._admin_counter}@testlab.io',
            password='testpass123!',
            first_name='Admin',
            last_name='User',
            role=Role.LAB_ADMIN,
        )

    def test_partners_isolated_between_tenants(
        self, second_tenant, make_request,
    ):
        admin_a = self._create_admin()

        # Create partner in tenant A
        PartnerOrganizationService.create(
            validated_data={
                'code': 'ISO-001',
                'name': 'Tenant A Clinic',
                'organization_type': OrganizationType.CLINIC,
            },
            created_by=admin_a,
            request=make_request(admin_a),
        )
        assert PartnerOrganization.objects.filter(code='ISO-001').count() == 1

        # Create user and partner in tenant B
        with schema_context(second_tenant):
            user_b = StaffUser.objects.create_user(
                email='admin@otherlab.io',
                password='testpass123!',
                first_name='Admin',
                last_name='Other',
                role=Role.LAB_ADMIN,
            )
            PartnerOrganizationService.create(
                validated_data={
                    'code': 'ISO-002',
                    'name': 'Tenant B Hospital',
                    'organization_type': OrganizationType.HOSPITAL,
                },
                created_by=user_b,
                request=make_request(user_b),
            )

            # Tenant B sees only its own partner
            assert PartnerOrganization.objects.count() == 1
            assert PartnerOrganization.objects.filter(code='ISO-002').exists()
            assert not PartnerOrganization.objects.filter(code='ISO-001').exists()

        # Back in tenant A — only its own partner visible
        assert PartnerOrganization.objects.count() == 1
        assert PartnerOrganization.objects.filter(code='ISO-001').exists()
        assert not PartnerOrganization.objects.filter(code='ISO-002').exists()

    def test_same_code_allowed_across_tenants(
        self, second_tenant, make_request,
    ):
        """Code uniqueness is per-tenant, not global."""
        admin_a = self._create_admin()

        PartnerOrganizationService.create(
            validated_data={
                'code': 'SHARED-CODE',
                'name': 'In Tenant A',
                'organization_type': OrganizationType.CLINIC,
            },
            created_by=admin_a,
            request=make_request(admin_a),
        )

        with schema_context(second_tenant):
            user_b = StaffUser.objects.create_user(
                email='admin2@otherlab.io',
                password='testpass123!',
                first_name='Admin2',
                last_name='Other',
                role=Role.LAB_ADMIN,
            )
            partner = PartnerOrganizationService.create(
                validated_data={
                    'code': 'SHARED-CODE',
                    'name': 'In Tenant B',
                    'organization_type': OrganizationType.HOSPITAL,
                },
                created_by=user_b,
                request=make_request(user_b),
            )
            assert partner.code == 'SHARED-CODE'
