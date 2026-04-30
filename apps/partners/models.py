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

PartnerExamPrice
    Agreed price negotiated between a partner and the lab for a given
    exam definition. Referenced by request creation (in a future step)
    to resolve ``billed_price`` automatically when the request source is
    PARTNER_ORGANIZATION.
"""
from django.db import models
from django.db.models import Q

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
    invoice_discount_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Global invoice discount percentage (e.g. 10.00 for 10%). '
                  'Applied on the gross total of generated invoices. '
                  'Distinct from per-exam negotiated prices.',
    )
    billing_notes = models.TextField(
        blank=True,
        default='',
        help_text='Internal notes on billing arrangements.',
    )

    # ---- Operational ----
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)

    # ---- Optional report branding (per-partner) ----
    # When ``custom_report_branding_enabled`` is True AND the relevant
    # field is non-empty, the result PDF for requests sourced from this
    # partner uses the partner's identity instead of the lab's. Each
    # field falls back to the lab's equivalent independently so a
    # partial configuration (e.g. partner name set, no logo) still
    # produces a clean report.
    custom_report_branding_enabled = models.BooleanField(
        default=False,
        help_text='When enabled, result PDFs for requests from this '
                  'partner use the partner-specific header/logo/footer '
                  'instead of the laboratory branding.',
    )
    report_header_name = models.CharField(max_length=255, blank=True, default='')
    report_header_subtitle = models.CharField(max_length=255, blank=True, default='')
    report_header_address = models.TextField(blank=True, default='')
    report_header_phone = models.CharField(max_length=50, blank=True, default='')
    report_header_email = models.EmailField(blank=True, default='')
    report_header_logo = models.ImageField(
        upload_to='partners/branding/logos/',
        blank=True,
        null=True,
        help_text='Partner logo printed on result PDFs. PNG or JPEG, '
                  'recommended at least 600px wide for crisp rendering.',
    )
    report_footer_text = models.TextField(
        blank=True, default='',
        help_text='Confidentiality / legal text printed at the bottom of '
                  'result PDFs in place of the lab footer.',
    )

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


class PartnerExamPrice(BaseModel):
    """
    Negotiated price for a specific (partner, exam_definition) pair.

    Intended consumption (implementation lives in a future step):
        - DIRECT_PATIENT requests → use ``ExamDefinition.unit_price``
        - PARTNER_ORGANIZATION requests → if an ACTIVE PartnerExamPrice
          exists for (partner, exam_definition), use its ``agreed_price``;
          otherwise fall back to ``ExamDefinition.unit_price``

    Historical integrity
    --------------------
    ``AnalysisRequestItem`` snapshots ``unit_price`` and ``billed_price``
    into its own columns at item creation time. Changing ``agreed_price``
    here therefore does **not** retroactively touch any existing request
    item — only future requests pick up the new value. The guarantee is
    enforced by the data model of requests, not by this module, which
    means this reference table is free to evolve without extra
    migration or backfill logic.

    Uniqueness
    ----------
    At most **one active** row per (partner, exam_definition) pair. The
    constraint is scoped to ``is_active=True`` (a partial unique index)
    so deactivated rows accumulate as history — the lab can deactivate
    an old agreed price and create a new one without losing the audit
    trail, and can reactivate an older negotiation as long as no other
    active row collides.
    """
    partner = models.ForeignKey(
        PartnerOrganization,
        on_delete=models.PROTECT,
        related_name='exam_prices',
    )
    exam_definition = models.ForeignKey(
        'catalog.ExamDefinition',
        on_delete=models.PROTECT,
        related_name='partner_prices',
    )
    agreed_price = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text='Negotiated unit price applied to requests from this partner.',
    )
    notes = models.TextField(
        blank=True,
        default='',
        help_text='Internal notes about the negotiation context or rationale.',
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Partner Exam Price'
        verbose_name_plural = 'Partner Exam Prices'
        ordering = ['-created_at']
        constraints = [
            # One active row per (partner, exam) pair. Deactivated rows
            # are excluded so history can coexist with a fresh
            # renegotiation. Enforced at the DB level — a race that
            # slips past serializer validation still hits this.
            models.UniqueConstraint(
                fields=['partner', 'exam_definition'],
                condition=Q(is_active=True),
                name='unique_active_partner_exam_price',
            ),
            models.CheckConstraint(
                check=Q(agreed_price__gte=0),
                name='partner_exam_price_non_negative',
            ),
        ]
        indexes = [
            models.Index(fields=['partner', 'is_active']),
            models.Index(fields=['exam_definition', 'is_active']),
        ]

    def __str__(self):
        return f'{self.partner.code} → {self.exam_definition.code} @ {self.agreed_price}'
