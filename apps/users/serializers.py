"""
Cytova — Users Serializers
"""
from rest_framework import serializers
from .models import StaffUser, Role, UserPermissionOverride


class StaffUserListSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = StaffUser
        fields = ['id', 'email', 'title', 'first_name', 'last_name', 'full_name', 'role', 'is_active', 'created_at']

    def get_full_name(self, obj):
        return obj.get_full_name()


class StaffUserDetailSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    has_signature = serializers.SerializerMethodField()

    class Meta:
        model = StaffUser
        fields = [
            'id', 'email', 'title', 'first_name', 'last_name',
            'full_name', 'display_name',
            'role', 'is_active', 'has_signature',
            'created_by', 'created_at', 'updated_at',
        ]

    def get_full_name(self, obj):
        return obj.get_full_name()

    def get_display_name(self, obj):
        return obj.get_display_name()

    def get_has_signature(self, obj):
        return bool(obj.signature_file_key)

    def get_created_by(self, obj):
        if obj.created_by_id:
            return {
                'id': str(obj.created_by_id),
                'email': obj.created_by.email if obj.created_by else None,
            }
        return None


class StaffUserCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    role = serializers.ChoiceField(choices=Role.choices)
    password = serializers.CharField(
        write_only=True,
        min_length=8,
        style={'input_type': 'password'},
    )

    def validate_email(self, value):
        if StaffUser.objects.filter(email=value).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return value


class StaffUserUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=20, required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=100, required=False)
    last_name = serializers.CharField(max_length=100, required=False)
    role = serializers.ChoiceField(choices=Role.choices, required=False)


class MeSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    has_signature = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = StaffUser
        fields = [
            'id', 'email', 'title', 'first_name', 'last_name',
            'full_name', 'display_name',
            'role', 'is_active', 'has_signature',
            'created_at', 'updated_at', 'permissions',
        ]

    def get_full_name(self, obj):
        return obj.get_full_name()

    def get_display_name(self, obj):
        return obj.get_display_name()

    def get_has_signature(self, obj):
        return bool(obj.signature_file_key)

    def get_permissions(self, obj):
        from common.permission_checker import PermissionChecker
        return sorted(PermissionChecker.get_effective_permissions(obj))


class MeUpdateSerializer(serializers.Serializer):
    """
    Self-update: change name fields and/or rotate password.
    Password change requires the current password for verification.
    Email is not changeable via this endpoint (LAB_ADMIN action).
    """
    first_name = serializers.CharField(max_length=100, required=False)
    last_name = serializers.CharField(max_length=100, required=False)
    current_password = serializers.CharField(write_only=True, required=False)
    new_password = serializers.CharField(write_only=True, min_length=8, required=False)

    def validate(self, attrs):
        new_password = attrs.get('new_password')
        current_password = attrs.get('current_password')

        if new_password and not current_password:
            raise serializers.ValidationError(
                {'current_password': 'Current password is required to set a new password.'}
            )
        if new_password:
            user = self.context['request'].user
            if not user.check_password(current_password):
                raise serializers.ValidationError(
                    {'current_password': 'Current password is incorrect.'}
                )
        return attrs


class SignatureUploadSerializer(serializers.Serializer):
    """Validate a signature image upload."""
    file = serializers.FileField()

    _ALLOWED_TYPES = frozenset({'image/png', 'image/jpeg', 'image/gif'})
    _MAX_SIZE = 2 * 1024 * 1024

    def validate_file(self, value):
        ct = getattr(value, 'content_type', '')
        if ct not in self._ALLOWED_TYPES:
            raise serializers.ValidationError(
                f'Unsupported image type: {ct}. Use PNG, JPEG or GIF.'
            )
        if value.size > self._MAX_SIZE:
            raise serializers.ValidationError('File too large. Maximum 2 MB.')
        return value


# ---------------------------------------------------------------------------
# RBAC serializers
# ---------------------------------------------------------------------------

class RoleAssignSerializer(serializers.Serializer):
    """Validate a role assignment request."""
    role = serializers.ChoiceField(choices=Role.choices)


class PermissionOverrideSerializer(serializers.Serializer):
    """Validate a permission override request (grant / revoke / remove)."""
    action = serializers.ChoiceField(choices=['grant', 'revoke', 'remove'])
    permission_code = serializers.CharField(max_length=80)
    reason = serializers.CharField(max_length=255, required=False, default='')

    def validate_permission_code(self, value):
        from common.permissions_registry import PermissionRegistry
        if not PermissionRegistry.is_valid(value):
            raise serializers.ValidationError(f'Unknown permission: {value}')
        return value


class UserPermissionOverrideSerializer(serializers.ModelSerializer):
    """Read-only serializer for displaying permission overrides."""
    granted_by_email = serializers.SerializerMethodField()

    class Meta:
        model = UserPermissionOverride
        fields = [
            'id', 'permission_code', 'override_type',
            'granted_by_email', 'reason', 'created_at',
        ]

    def get_granted_by_email(self, obj):
        return obj.granted_by.email if obj.granted_by else None
