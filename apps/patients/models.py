"""
Cytova — Patient Models

Patient
    Per-tenant patient record. Identified by document_type + document_number
    (unique together within the schema). Hard delete is blocked at the model
    level (BR-P2); only deactivation is permitted once the patient has linked
    analysis requests.

PatientPortalAccount
    Optional 1:1 companion to Patient. Grants the patient read-only access
    to their own published results via the patient portal. Uses AbstractBaseUser
    for password management (set_password / check_password), but is NOT
    registered as AUTH_USER_MODEL — it is authenticated via a dedicated
    PatientPortalAuthentication class (portal module, future phase).
"""
import uuid
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone

from common.models import BaseModel


class Gender(models.TextChoices):
    MALE = 'MALE', 'Male'
    FEMALE = 'FEMALE', 'Female'


class DocumentType(models.TextChoices):
    NATIONAL_ID_CARD = 'NATIONAL_ID_CARD', 'National ID Card'
    PASSPORT = 'PASSPORT', 'Passport'
    CIP = 'CIP', 'CIP'
    RESIDENCE_PERMIT = 'RESIDENCE_PERMIT', 'Residence Permit'
    OTHER = 'OTHER', 'Other'
    # Explicit "no document on file" marker. When the operator picks
    # UNKNOWN and leaves the number blank, the service auto-generates
    # a technical identifier (``AUTO-PT-YYYYMMDD-XXXXXX``) and stamps
    # ``identity_number_auto_generated=True`` so the UI can
    # distinguish a real ID from a placeholder. Distinct from
    # ``OTHER`` (which still expects a real-but-uncategorised
    # number).
    UNKNOWN = 'UNKNOWN', 'Unknown / not provided'


class Patient(BaseModel):
    """
    A person whose biological samples are analysed by the laboratory.
    Scoped to the current tenant schema — no cross-tenant visibility.
    """
    # Identification
    document_type = models.CharField(
        max_length=20,
        choices=DocumentType.choices,
        default=DocumentType.NATIONAL_ID_CARD,
    )
    document_number = models.CharField(max_length=100, db_index=True)
    identity_number_auto_generated = models.BooleanField(
        default=False,
        help_text='True when ``document_number`` was auto-generated '
                  'by the service (typically because the operator '
                  'picked ``DocumentType.UNKNOWN`` and left the number '
                  'blank). The UI uses this to render the value as a '
                  'placeholder rather than a real ID — surfacing it as '
                  'a real ID would mislead downstream operators and '
                  'patients about the document\'s provenance.',
    )

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    # ``date_of_birth`` was historically NOT NULL. The
    # flexible-identity rollout relaxes this to allow patients
    # whose DOB the lab simply doesn't have on file (typically
    # samples received from partners with incomplete metadata).
    # Pair it with ``date_of_birth_unknown`` so the UI can
    # distinguish "missing" from "0000-00-00" or sentinel dates;
    # validators refuse a null DOB unless the flag is explicitly
    # set, so a forgotten field cannot land null by accident.
    date_of_birth = models.DateField(null=True, blank=True)
    date_of_birth_unknown = models.BooleanField(
        default=False,
        help_text='True when the lab confirms the DOB is unavailable. '
                  'When true, ``date_of_birth`` may be null. When false, '
                  'validators require ``date_of_birth`` to be set.',
    )
    gender = models.CharField(max_length=10, choices=Gender.choices)
    nationality = models.CharField(max_length=100, blank=True, default='')

    # Contact / location
    phone = models.CharField(max_length=30, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    city_of_residence = models.CharField(max_length=150, blank=True, default='')
    address = models.TextField(blank=True, default='')

    # Billing
    insurance_number = models.CharField(max_length=100, blank=True, default='')

    is_active = models.BooleanField(default=True, db_index=True)

    created_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_patients',
    )

    # ---- Cytova patient identity link --------------------------------
    # Snapshot of a verified link between this tenant-local Patient and
    # a global ``apps.patient_portal.PatientAccount``. The link is set
    # by the lab once, after the receptionist enters the patient's
    # Cytova credentials and the global identity-verification service
    # confirms a match. Once linked, Notify Cytova and any other
    # patient-portal-bound action can reuse the verified identity
    # without re-prompting the operator for the patient's name / DOB.
    #
    # Cross-schema reference rules:
    #   - ``cytova_patient_account_id`` is a UUID *snapshot* — never
    #     a foreign key. ``PatientAccount`` lives in the public
    #     schema; cross-schema FKs aren't supported by django-tenants
    #     and would also be a layering violation. Validity is
    #     re-checked at use time (e.g. before Notify Cytova actually
    #     shares).
    #   - ``cytova_patient_id`` mirrors what the patient sees on their
    #     side (CV-XXXX-XXXX, 12 chars). It's the human-readable
    #     reference an operator can quote on the phone, and the value
    #     the lab UI surfaces on the linked-state badge.
    #
    # Uniqueness:
    #   - Two local patients pointing at the same global Cytova ID
    #     would almost certainly indicate an operator error or stale
    #     record. We enforce that with a partial unique constraint
    #     covering only non-empty values, so unlinked rows (the
    #     default) don't compete on the empty-string default.
    cytova_patient_id = models.CharField(
        max_length=12, blank=True, default='', db_index=True,
        help_text='Snapshot of the global Cytova Patient ID '
                  '(CV-XXXX-XXXX) once a verified link exists. '
                  'Empty when the patient is not linked.',
    )
    cytova_patient_account_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Snapshot of the global PatientAccount UUID at '
                  'link time. NOT a foreign key — patient_portal '
                  'tables live in the public schema. Validity is '
                  're-checked at use time.',
    )
    cytova_identity_verified_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the link was created via the global identity '
                  'verification service.',
    )
    cytova_identity_verified_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cytova_links_verified',
        help_text='Staff member who performed the link.',
    )
    cytova_identity_unlinked_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the link was last cleared. Survives a '
                  'subsequent re-link so the audit trail is '
                  'continuous; the link service stamps a fresh '
                  'verified_at on relink.',
    )
    cytova_identity_unlinked_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cytova_links_unlinked',
    )

    class Meta:
        verbose_name = 'Patient'
        verbose_name_plural = 'Patients'
        ordering = ['last_name', 'first_name']
        indexes = [
            models.Index(fields=['last_name', 'first_name']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['document_type', 'document_number'],
                name='unique_patient_document',
            ),
            # Partial unique: one local patient per global Cytova ID
            # within this tenant schema. The ``exclude empty`` clause
            # is critical — every unlinked row carries the empty
            # string (the field's default), and a plain unique index
            # would refuse to keep more than one of them.
            models.UniqueConstraint(
                fields=['cytova_patient_id'],
                condition=~models.Q(cytova_patient_id=''),
                name='unique_patient_cytova_id_when_set',
            ),
        ]

    def __str__(self):
        return f'{self.last_name.upper()}, {self.first_name} ({self.document_number})'

    def delete(self, *args, **kwargs):
        """
        BR-P2: patient records cannot be hard-deleted.
        Use deactivation (is_active=False) instead.
        """
        raise PermissionError(
            'Patient records cannot be deleted. Use deactivation instead.'
        )

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def has_portal_account(self):
        return hasattr(self, 'portal_account') and self.portal_account is not None

    @property
    def has_cytova_identity(self) -> bool:
        """True iff the patient has been linked to a global Cytova
        account. Both halves of the snapshot must be populated; an
        unlinked or partially-cleared row reads as False so the UI
        and Notify Cytova flow never act on half-state."""
        return bool(self.cytova_patient_id) and self.cytova_patient_account_id is not None


class PatientPortalAccountManager(BaseUserManager):
    def create(self, patient, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required for a portal account.')
        account = self.model(
            patient=patient,
            email=self.normalize_email(email),
            **extra_fields,
        )
        account.set_password(password)
        account.save(using=self._db)
        return account


class PatientPortalAccount(AbstractBaseUser):
    """
    Optional portal account linked 1:1 to a Patient.

    Provides patients with read-only access to their own published results.
    Email space is independent from StaffUser email space — uniqueness is
    enforced only within this table (DOMAIN_MODEL.md §patient_portal_accounts).

    Authentication for the portal is handled by PatientPortalAuthentication
    (to be implemented in the portal module). This model focuses on lifecycle:
    creation, deactivation, and password management.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.OneToOneField(
        Patient,
        on_delete=models.CASCADE,
        related_name='portal_account',
    )
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_portal_accounts',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    # AbstractBaseUser expects these
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = PatientPortalAccountManager()

    class Meta:
        verbose_name = 'Patient Portal Account'
        verbose_name_plural = 'Patient Portal Accounts'

    def __str__(self):
        return f'Portal: {self.email} → {self.patient}'
