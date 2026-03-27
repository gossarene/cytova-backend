"""
Cytova — Users Service

Business logic for staff user lifecycle: create, update, deactivate, activate.
All critical actions produce an AuditLog record.
Views stay thin — they validate input and delegate here.
"""
import logging

from apps.users.models import StaffUser
from apps.audit.models import AuditLog, AuditAction, ActorType

logger = logging.getLogger(__name__)


class UserService:

    @staticmethod
    def create_user(validated_data: dict, created_by: StaffUser, request) -> StaffUser:
        """
        Create a new staff user within the current tenant schema.
        The temporary password is set immediately; the user should change it
        on first login (password-reset flow).
        """
        password = validated_data.pop('password')
        user = StaffUser(created_by=created_by, **validated_data)
        user.set_password(password)
        user.save()

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
        """Set is_active=False. Idempotent."""
        if not user.is_active:
            return user

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
