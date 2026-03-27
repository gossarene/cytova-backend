"""
Cytova — Patient Service

Handles patient lifecycle (create, update, deactivate) and portal account
management (create, delete). All write operations produce AuditLog records.
"""
import logging

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.users.models import StaffUser
from common.utils.crypto import generate_secure_token
from .models import Patient, PatientPortalAccount

logger = logging.getLogger(__name__)


class PatientService:

    @staticmethod
    def create_patient(validated_data: dict, created_by: StaffUser, request) -> Patient:
        patient = Patient(created_by=created_by, **validated_data)
        patient.save()

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='Patient',
            entity_id=patient.id,
            diff={'after': {
                'national_id': patient.national_id,
                'first_name': patient.first_name,
                'last_name': patient.last_name,
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return patient

    @staticmethod
    def update_patient(
        patient: Patient,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> Patient:
        before = {k: getattr(patient, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(patient, field, value)
        patient.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(patient, k) for k in validated_data}

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=updated_by.id,
            actor_email=updated_by.email,
            action=AuditAction.UPDATE,
            entity_type='Patient',
            entity_id=patient.id,
            diff={'before': before, 'after': after},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return patient

    @staticmethod
    def deactivate_patient(
        patient: Patient,
        deactivated_by: StaffUser,
        request,
    ) -> Patient:
        """Idempotent. Sets is_active=False and writes an audit record."""
        if not patient.is_active:
            return patient

        patient.is_active = False
        patient.save(update_fields=['is_active', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=deactivated_by.id,
            actor_email=deactivated_by.email,
            action=AuditAction.DEACTIVATE,
            entity_type='Patient',
            entity_id=patient.id,
            diff={'after': {'is_active': False}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return patient

    @staticmethod
    def create_portal_account(
        patient: Patient,
        email: str,
        created_by: StaffUser,
        request,
    ) -> PatientPortalAccount:
        """
        Create a PatientPortalAccount for the given patient.
        A temporary password is generated and should be sent via email.
        The plaintext password is never persisted — only the hash is stored.
        """
        temp_password = generate_secure_token(length=16)

        account = PatientPortalAccount.objects.create(
            patient=patient,
            email=email,
            created_by=created_by,
            password=temp_password,  # set_password called in manager.create()
        )

        # TODO: dispatch send_portal_welcome_email(account.id, temp_password)
        #       Celery task — email module (future phase)
        logger.info(
            'Portal account created for patient %s (email: %s)',
            patient.id, email,
        )

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='PatientPortalAccount',
            entity_id=account.id,
            diff={'after': {'email': email, 'patient_id': str(patient.id)}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return account

    @staticmethod
    def delete_portal_account(
        account: PatientPortalAccount,
        deleted_by: StaffUser,
        request,
    ) -> None:
        """
        Remove the portal account. The patient record is unaffected.
        Uses hard delete — portal accounts have no audit trail of their own
        beyond this event.
        """
        patient_id = account.patient_id
        account_id = account.id
        email = account.email

        # Use underlying queryset delete to bypass model-level restrictions
        PatientPortalAccount.objects.filter(id=account.id).delete()

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=deleted_by.id,
            actor_email=deleted_by.email,
            action=AuditAction.DELETE,
            entity_type='PatientPortalAccount',
            entity_id=account_id,
            diff={'before': {'email': email, 'patient_id': str(patient_id)}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )
