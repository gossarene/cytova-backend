"""
Cytova — Users Service

Business logic for staff user lifecycle: create, update, deactivate, activate.
Role assignment and permission override management.
All critical actions produce an AuditLog record.
Views stay thin — they validate input and delegate here.
"""
import logging

from django.core.exceptions import ValidationError

from apps.users.models import StaffUser, Role, UserPermissionOverride, OverrideType
from apps.audit.models import AuditLog, AuditAction, ActorType
from common.permission_checker import PermissionChecker
from common.permissions_registry import PermissionRegistry

logger = logging.getLogger(__name__)


class UserService:

    @staticmethod
    def create_user(validated_data: dict, created_by: StaffUser, request) -> StaffUser:
        """
        Create a new staff user within the current tenant schema.
        The temporary password is set immediately; the user should change it
        on first login (password-reset flow).

        Routes through ``StaffUser.objects.create_user`` so that role-derived
        notification defaults are applied when the caller didn't explicitly
        supply ``receive_review_ready_notifications`` /
        ``receive_result_rejection_notifications``.
        """
        password = validated_data.pop('password')
        email = validated_data.pop('email')
        user = StaffUser.objects.create_user(
            email=email,
            password=password,
            created_by=created_by,
            **validated_data,
        )

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='StaffUser',
            entity_id=user.id,
            diff={'after': {'email': user.email, 'role': user.role}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return user

    @staticmethod
    def update_user(user: StaffUser, validated_data: dict, updated_by: StaffUser, request) -> StaffUser:
        """Update mutable fields (name, role) on a staff user."""
        before = {k: getattr(user, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(user, field, value)
        user.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(user, k) for k in validated_data}

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=updated_by.id,
            actor_email=updated_by.email,
            action=AuditAction.UPDATE,
            entity_type='StaffUser',
            entity_id=user.id,
            diff={'before': before, 'after': after},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return user

    @staticmethod
    def update_me(user: StaffUser, validated_data: dict, request) -> StaffUser:
        """Self-update: name fields and optional password rotation."""
        new_password = validated_data.pop('new_password', None)
        validated_data.pop('current_password', None)

        before = {k: getattr(user, k) for k in validated_data}
        update_fields = []

        for field, value in validated_data.items():
            setattr(user, field, value)
            update_fields.append(field)

        if new_password:
            user.set_password(new_password)
            update_fields.append('password')

        if update_fields:
            update_fields.append('updated_at')
            user.save(update_fields=update_fields)

        after = {k: getattr(user, k) for k in before}

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=user.id,
            actor_email=user.email,
            action=AuditAction.UPDATE,
            entity_type='StaffUser',
            entity_id=user.id,
            diff={'before': before, 'after': after},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return user

    @staticmethod
    def deactivate_user(user: StaffUser, deactivated_by: StaffUser, request) -> StaffUser:
        """Set is_active=False. Idempotent. Prevents deactivating the last lab admin."""
        if not user.is_active:
            return user

        # Last-admin protection
        if user.role == Role.LAB_ADMIN:
            remaining = StaffUser.objects.filter(
                role=Role.LAB_ADMIN, is_active=True,
            ).exclude(id=user.id).count()
            if remaining == 0:
                raise ValidationError(
                    'Cannot deactivate the last active Lab Admin. '
                    'Assign another Lab Admin first.'
                )

        user.is_active = False
        user.save(update_fields=['is_active', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=deactivated_by.id,
            actor_email=deactivated_by.email,
            action=AuditAction.DEACTIVATE,
            entity_type='StaffUser',
            entity_id=user.id,
            diff={'after': {'is_active': False}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return user

    @staticmethod
    def activate_user(user: StaffUser, activated_by: StaffUser, request) -> StaffUser:
        """Set is_active=True. Idempotent."""
        if user.is_active:
            return user

        user.is_active = True
        user.save(update_fields=['is_active', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=activated_by.id,
            actor_email=activated_by.email,
            action=AuditAction.UPDATE,
            entity_type='StaffUser',
            entity_id=user.id,
            diff={'after': {'is_active': True}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return user

    # ------------------------------------------------------------------
    # Role assignment
    # ------------------------------------------------------------------

    @staticmethod
    def assign_role(
        user: StaffUser,
        new_role: str,
        assigned_by: StaffUser,
        request,
    ) -> StaffUser:
        """
        Change a user's role. Enforces:
        - Cannot demote the last active LAB_ADMIN
        - Clears all permission overrides on role change (they were for the old role)
        - Audit-logs every role change
        """
        old_role = user.role

        if old_role == new_role:
            return user

        # Last-admin protection
        if old_role == Role.LAB_ADMIN and new_role != Role.LAB_ADMIN:
            remaining = StaffUser.objects.filter(
                role=Role.LAB_ADMIN, is_active=True,
            ).exclude(id=user.id).count()
            if remaining == 0:
                raise ValidationError(
                    'Cannot remove the last active Lab Admin. '
                    'Assign another Lab Admin first.'
                )

        user.role = new_role
        user.save(update_fields=['role', 'updated_at'])

        # Clear overrides — they were granted for the old role context
        user.permission_overrides.all().delete()
        PermissionChecker.invalidate_cache(user)

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=assigned_by.id,
            actor_email=assigned_by.email,
            action=AuditAction.ROLE_ASSIGN,
            entity_type='StaffUser',
            entity_id=user.id,
            diff={
                'before': {'role': old_role},
                'after': {'role': new_role},
            },
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return user

    # ------------------------------------------------------------------
    # Permission overrides
    # ------------------------------------------------------------------

    @staticmethod
    def grant_permission(
        user: StaffUser,
        permission_code: str,
        granted_by: StaffUser,
        reason: str,
        request,
    ) -> UserPermissionOverride:
        """Grant an additional permission to a user beyond their role defaults."""
        if not PermissionRegistry.is_valid(permission_code):
            raise ValidationError(f'Unknown permission code: {permission_code}')

        override, _created = UserPermissionOverride.objects.update_or_create(
            user=user,
            permission_code=permission_code,
            defaults={
                'override_type': OverrideType.GRANT,
                'granted_by': granted_by,
                'reason': reason,
            },
        )

        PermissionChecker.invalidate_cache(user)

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=granted_by.id,
            actor_email=granted_by.email,
            action=AuditAction.PERMISSION_OVERRIDE,
            entity_type='UserPermissionOverride',
            entity_id=override.id,
            diff={
                'permission': permission_code,
                'type': 'GRANT',
                'target_user': str(user.id),
                'target_email': user.email,
            },
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return override

    @staticmethod
    def revoke_permission(
        user: StaffUser,
        permission_code: str,
        granted_by: StaffUser,
        reason: str,
        request,
    ) -> UserPermissionOverride:
        """Revoke a specific permission from a user (removes from role defaults)."""
        if not PermissionRegistry.is_valid(permission_code):
            raise ValidationError(f'Unknown permission code: {permission_code}')

        override, _created = UserPermissionOverride.objects.update_or_create(
            user=user,
            permission_code=permission_code,
            defaults={
                'override_type': OverrideType.REVOKE,
                'granted_by': granted_by,
                'reason': reason,
            },
        )

        PermissionChecker.invalidate_cache(user)

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=granted_by.id,
            actor_email=granted_by.email,
            action=AuditAction.PERMISSION_OVERRIDE,
            entity_type='UserPermissionOverride',
            entity_id=override.id,
            diff={
                'permission': permission_code,
                'type': 'REVOKE',
                'target_user': str(user.id),
                'target_email': user.email,
            },
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return override

    @staticmethod
    def remove_permission_override(
        user: StaffUser,
        permission_code: str,
        removed_by: StaffUser,
        request,
    ):
        """Remove an override entirely, restoring the role default for that permission."""
        try:
            override = UserPermissionOverride.objects.get(
                user=user, permission_code=permission_code,
            )
        except UserPermissionOverride.DoesNotExist:
            raise ValidationError('No override found for this permission.')

        old_type = override.override_type
        override_id = override.id
        override.delete()

        PermissionChecker.invalidate_cache(user)

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=removed_by.id,
            actor_email=removed_by.email,
            action=AuditAction.PERMISSION_OVERRIDE,
            entity_type='UserPermissionOverride',
            entity_id=override_id,
            diff={
                'permission': permission_code,
                'type': 'REMOVED',
                'previous_override': old_type,
                'target_user': str(user.id),
                'target_email': user.email,
            },
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )
