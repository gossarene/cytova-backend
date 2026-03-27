"""
Cytova — Partner Organization Models

PartnerOrganization
    A healthcare entity that sends analysis requests to the laboratory.
    Covers clinics, hospitals, partner laboratories, medical centres, and
    other referring bodies.

    `code` is a short stable identifier (unique, uppercase) used in
    reporting, billing references, and cross-module linking. Stored
    uppercase by the service layer.

    `organization_type` classifies the partner for analytics and
    billing-rule selection (future).

    `default_billing_mode` and `payment_terms_days` prepare the ground
    for invoicing without implementing it — these fields carry no
    application logic today.

    Hard delete is blocked — use deactivation. Partners are referenced
    from analysis requests and future billing records.
"""
from django.db import models

from common.models import BaseModel


class OrganizationType(models.TextChoices):
    CLINIC          = 'CLINIC',          'Clinic'
    HOSPITAL        = 'HOSPITAL',        'Hospital'
    LABORATORY      = 'LABORATORY',      'Partner Laboratory'
    MEDICAL_CENTER  = 'MEDICAL_CENTER',  'Medical Center'
    OTHER           = 'OTHER',           'Other'


class BillingMode(models.TextChoices):
    PREPAID    = 'PREPAID',    'Prepaid'
    ON_ACCOUNT = 'ON_ACCOUNT', 'On Account (invoiced)'
    PER_REQUEST = 'PER_REQUEST', 'Per Request'


class PartnerOrganization(BaseModel):
    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text='Short stable identifier (uppercase). Used in reporting and billing.',
    )
    name = models.CharField(max_length=255)
    organization_type = models.CharField(
        max_length=20,
        choices=OrganizationType.choices,
        db_index=True,
    )

    # ---- Contact ----
    contact_person = models.CharField(max_length=255, blank=True, default='')
    phone = models.CharField(max_length=50, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    address = models.TextField(blank=True, default='')

    # ---- Billing (future-ready, no logic today) ----
    default_billing_mode = models.CharField(
        max_length=15,
        choices=BillingMode.choices,
        null=True,
        blank=True,
        help_text='Default billing mode for requests from this partner.',
    )
    payment_terms_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Payment terms in days (e.g. 30, 60). Used by future invoicing module.',
    )
    billing_notes = models.TextField(
        blank=True,
        default='',
        help_text='Internal notes on billing arrangements.',
    )

    # ---- Operational ----
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Partner Organization'
        verbose_name_plural = 'Partner Organizations'
        ordering = ['name']
        indexes = [
            models.Index(fields=['organization_type', 'is_active']),
        ]

    def __str__(self):
        return f'[{self.code}] {self.name}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Partner organizations cannot be deleted. Use deactivation instead.'
        )
