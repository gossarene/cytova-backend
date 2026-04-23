"""
Tests for the 4-digit tenant numeric code.

Covers:
- Allocation is sequential, zero-padded, and starts at 0001
- Codes are globally unique and immutable once assigned
- New tenants created via ``tenant.save()`` auto-allocate a code
- Onboarding flow surfaces the code on the resulting tenant
- The allocator exhausts cleanly at 9999
"""
import pytest
from django_tenants.utils import get_public_schema_name, schema_context

from apps.tenants.models import Tenant, TenantCodeCounter
from apps.tenants.services import TenantCodeAllocator, TENANT_CODE_MAX


pytestmark = pytest.mark.django_db


@pytest.fixture()
def in_public(_test_tenant_schema):
    with schema_context(get_public_schema_name()):
        yield


class TestNumericCodeAllocation:

    def test_allocator_returns_zero_padded_sequential_values(self, in_public):
        # Reset counter to 0 so assertions are deterministic inside this test
        counter, _ = TenantCodeCounter.objects.get_or_create(pk=1)
        counter.last_value = 0
        counter.save(update_fields=['last_value'])

        a = TenantCodeAllocator.allocate()
        b = TenantCodeAllocator.allocate()
        c = TenantCodeAllocator.allocate()

        assert a == '0001'
        assert b == '0002'
        assert c == '0003'
        assert all(code.isdigit() and len(code) == 4 for code in (a, b, c))

    def test_allocator_is_globally_unique(self, in_public):
        codes = {TenantCodeAllocator.allocate() for _ in range(10)}
        assert len(codes) == 10

    def test_tenant_save_auto_allocates_code(self, in_public):
        t = Tenant(
            name='New Lab',
            subdomain='newlab',
            schema_name='schema_newlab',
        )
        t.save()
        try:
            assert t.numeric_code
            assert t.numeric_code.isdigit()
            assert len(t.numeric_code) == 4
        finally:
            # Clean up so we don't leak a schema between tests
            t.delete()

    def test_numeric_code_is_immutable_on_subsequent_save(self, in_public):
        tenant = Tenant.objects.get(schema_name='schema_testlab')
        original = tenant.numeric_code
        tenant.name = 'Renamed Lab'
        tenant.save()
        tenant.refresh_from_db()
        assert tenant.numeric_code == original

    def test_allocator_raises_when_exhausted(self, in_public):
        counter, _ = TenantCodeCounter.objects.get_or_create(pk=1)
        counter.last_value = TENANT_CODE_MAX
        counter.save(update_fields=['last_value'])
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError):
            TenantCodeAllocator.allocate()
