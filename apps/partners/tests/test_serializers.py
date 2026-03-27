"""
Tests for PartnerOrganization serializer validation.
"""
from apps.partners.models import OrganizationType
from apps.partners.serializers import PartnerOrganizationCreateSerializer


class TestPartnerOrganizationCreateSerializer:

    def test_valid_minimal(self):
        s = PartnerOrganizationCreateSerializer(data={
            'code': 'MIN-001',
            'name': 'Minimal Partner',
            'organization_type': OrganizationType.CLINIC,
        })
        assert s.is_valid(), s.errors

    def test_code_uppercased(self):
        s = PartnerOrganizationCreateSerializer(data={
            'code': 'lower-case',
            'name': 'Test',
            'organization_type': OrganizationType.HOSPITAL,
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['code'] == 'LOWER-CASE'

    def test_duplicate_code_rejected(self, lab_admin, make_request):
        from apps.partners.services import PartnerOrganizationService

        PartnerOrganizationService.create(
            validated_data={
                'code': 'DUP-001',
                'name': 'First',
                'organization_type': OrganizationType.CLINIC,
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        s = PartnerOrganizationCreateSerializer(data={
            'code': 'DUP-001',
            'name': 'Second',
            'organization_type': OrganizationType.CLINIC,
        })
        assert not s.is_valid()
        assert 'code' in s.errors

    def test_invalid_organization_type(self):
        s = PartnerOrganizationCreateSerializer(data={
            'code': 'BAD-001',
            'name': 'Bad Type',
            'organization_type': 'PHARMACY',
        })
        assert not s.is_valid()
        assert 'organization_type' in s.errors

    def test_missing_required_fields(self):
        s = PartnerOrganizationCreateSerializer(data={})
        assert not s.is_valid()
        assert 'code' in s.errors
        assert 'name' in s.errors
        assert 'organization_type' in s.errors
