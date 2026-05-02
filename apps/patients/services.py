"""
Cytova — Patient Service

Handles patient lifecycle (create, update, deactivate) and portal account
management (create, delete). All write operations produce AuditLog records.
"""
from datetime import date
import logging

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.users.models import StaffUser
from common.utils.crypto import generate_secure_token
from .models import Patient, PatientPortalAccount

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cytova-link exception types
# ---------------------------------------------------------------------------
#
# Mirror the shape of ``apps.requests.notify_cytova_service`` so the view
# layer can map them to consistent HTTP envelopes. Code constants stay
# stable across releases — clients can branch on them.

class CytovaLinkError(Exception):
    """Base for the typed errors the link/unlink view maps to HTTP."""
    code: str = 'CYTOVA_LINK_ERROR'
    message: str = 'Could not update Cytova identity link.'


class IdentityVerificationFailed(CytovaLinkError):
    """Single non-distinguishing failure for any verification mismatch
    (unknown Cytova ID, wrong name, wrong DOB, inactive global account).
    Mirrors the Notify-Cytova policy: never tell the lab user which
    field failed — that would turn the lookup into an enumeration
    oracle for global patient identity."""
    code = 'IDENTITY_VERIFICATION_FAILED'
    message = (
        'Identity verification failed. Please check the Cytova ID or '
        'patient information.'
    )


class AlreadyLinked(CytovaLinkError):
    """Refused because the patient already has an active Cytova link.
    The operator must explicitly unlink first — keeps the audit
    lineage clean (no silent overwrite of a previously-verified
    identity) and prevents accidental re-pointing to a different
    global account."""
    code = 'ALREADY_LINKED'
    message = (
        'This patient is already linked to a Cytova account. '
        'Unlink first to change the identity.'
    )


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
                'document_type': patient.document_type,
                'document_number': patient.document_number,
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

    # ------------------------------------------------------------------
    # Cytova patient-identity link
    # ------------------------------------------------------------------

    @staticmethod
    def link_cytova_identity(
        *,
        patient: Patient,
        cytova_patient_id: str,
        first_name: str,
        last_name: str,
        date_of_birth: date,
        actor: StaffUser,
        request,
    ) -> Patient:
        """Verify and persist a snapshot link from a tenant ``Patient``
        to the global ``PatientAccount`` matching the supplied Cytova
        identity.

        Verification is delegated to the existing
        ``apps.patient_portal.lookup.verify_patient_identity`` helper
        — the same call site Notify-Cytova uses — so the matching
        rules (case-insensitive name compare, exact DOB, single
        non-distinguishing failure) stay consistent across the two
        surfaces.

        Failure surfaces
        ----------------
        - ``AlreadyLinked``                — patient already has a
          link. Refuse without re-running verification (cheap
          pre-check; avoids spending a verification attempt on a
          no-op write). The operator must unlink first.
        - ``IdentityVerificationFailed``  — any mismatch (unknown ID,
          wrong name/DOB, inactive global account). Single code,
          single message — no information about which field failed
          ever leaves this function.

        Audit
        -----
        Successful link → ``PATIENT_CYTOVA_IDENTITY_LINKED`` row with
        the cytova_patient_id (already known to both sides), the
        patient_account_id snapshot (UUID string), and the local
        patient_id. Patient PII is never written to the audit log.

        Failed verification → ``UPDATE`` audit row with a truncated
        snapshot of the attempted Cytova ID so brute-force probing is
        observable. Mirrors the failed-Notify-Cytova policy.
        """
        actor_email = getattr(actor, 'email', '') or ''

        # Cheap pre-check — refuse a no-op verification on an
        # already-linked patient. Per the validated decision: the
        # operator must explicitly unlink before re-linking, so the
        # audit lineage stays continuous (no silent identity swap).
        if patient.has_cytova_identity:
            raise AlreadyLinked()

        # Identity verification — delegated to the patient_portal
        # module so the matching rules stay in one place. Returns the
        # PatientProfile on success or None on any failure mode.
        from apps.patient_portal.lookup import verify_patient_identity

        profile = verify_patient_identity(
            cytova_patient_id, first_name, last_name, date_of_birth,
        )
        if profile is None:
            # Brute-force-detection audit: record the attempt with
            # only the (already-public) Cytova ID. Truncated to keep
            # accidentally-pasted blobs out of the audit table.
            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=getattr(actor, 'id', None),
                actor_email=actor_email,
                action=AuditAction.UPDATE,
                entity_type='Patient',
                entity_id=patient.id,
                diff={'after': {
                    'cytova_link_outcome': 'IDENTITY_MISMATCH',
                    'cytova_patient_id_attempted': (
                        (cytova_patient_id or '').strip()[:32]
                    ),
                }},
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', '') or '',
            )
            raise IdentityVerificationFailed()

        # Snapshot + audit in one transaction so a failure halfway
        # through can never leave a half-linked state.
        now = timezone.now()
        with transaction.atomic():
            patient.cytova_patient_id = profile.cytova_patient_id
            patient.cytova_patient_account_id = profile.account_id
            patient.cytova_identity_verified_at = now
            patient.cytova_identity_verified_by = actor
            # Successful re-link clears the previous unlink stamp so
            # the row reflects the active state. The audit log keeps
            # the unlink/link history.
            patient.cytova_identity_unlinked_at = None
            patient.cytova_identity_unlinked_by = None
            patient.save(update_fields=[
                'cytova_patient_id', 'cytova_patient_account_id',
                'cytova_identity_verified_at', 'cytova_identity_verified_by',
                'cytova_identity_unlinked_at', 'cytova_identity_unlinked_by',
                'updated_at',
            ])

            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=getattr(actor, 'id', None),
                actor_email=actor_email,
                action=AuditAction.PATIENT_CYTOVA_IDENTITY_LINKED,
                entity_type='Patient',
                entity_id=patient.id,
                # Audit metadata: only IDs already known to both sides.
                # Patient name / DOB / email are NEVER written here —
                # they're already on the global PatientAccount and we
                # don't want a tenant-side copy living in the audit
                # table.
                diff={'after': {
                    'cytova_patient_id': profile.cytova_patient_id,
                    'cytova_patient_account_id': str(profile.account_id),
                }},
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', '') or '',
            )

        logger.info(
            'Patient linked to Cytova identity: patient_id=%s '
            'account_id=%s actor_id=%s',
            patient.id, profile.account_id, getattr(actor, 'id', None),
        )
        return patient

    @staticmethod
    def unlink_cytova_identity(
        *,
        patient: Patient,
        actor: StaffUser,
        request,
    ) -> Patient:
        """Clear the patient's Cytova link snapshot. Idempotent — a
        re-unlink on an already-unlinked patient is a no-op (no audit
        row, no error) so the UI can fire-and-forget without trying
        to track local state.

        Audit metadata captures the *previous* Cytova ID + account ID
        snapshot so the audit log keeps a complete chain even after
        the live row has been cleared. Both values are IDs already
        known to both sides — no patient PII is written.
        """
        if not patient.has_cytova_identity:
            # Idempotent no-op — matches the Notify-Cytova revoke
            # pattern. Returning the unchanged patient lets the view
            # render the same detail payload without a special branch.
            return patient

        actor_email = getattr(actor, 'email', '') or ''
        previous_cytova_id = patient.cytova_patient_id
        previous_account_id = patient.cytova_patient_account_id
        now = timezone.now()

        with transaction.atomic():
            patient.cytova_patient_id = ''
            patient.cytova_patient_account_id = None
            patient.cytova_identity_unlinked_at = now
            patient.cytova_identity_unlinked_by = actor
            # ``verified_at`` / ``verified_by`` are intentionally NOT
            # cleared — the historical truth (this patient *was*
            # verified at time T by user U) survives the unlink.
            # The new unlinked stamp marks when that link became
            # inactive. A subsequent re-link clears the unlinked
            # stamp and refreshes verified_at/by.
            patient.save(update_fields=[
                'cytova_patient_id', 'cytova_patient_account_id',
                'cytova_identity_unlinked_at', 'cytova_identity_unlinked_by',
                'updated_at',
            ])

            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=getattr(actor, 'id', None),
                actor_email=actor_email,
                action=AuditAction.PATIENT_CYTOVA_IDENTITY_UNLINKED,
                entity_type='Patient',
                entity_id=patient.id,
                diff={'before': {
                    'cytova_patient_id': previous_cytova_id,
                    'cytova_patient_account_id': str(previous_account_id),
                }},
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', '') or '',
            )

        logger.info(
            'Patient unlinked from Cytova identity: patient_id=%s '
            'previous_account_id=%s actor_id=%s',
            patient.id, previous_account_id, getattr(actor, 'id', None),
        )
        return patient

    # ------------------------------------------------------------------
    # Portal account lifecycle (legacy in-tenant local portal account —
    # distinct from the global Cytova patient portal)
    # ------------------------------------------------------------------

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
