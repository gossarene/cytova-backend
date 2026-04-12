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

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField()
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
