"""
Cytova — Catalog Models

ExamCategory
    Flat grouping of exam definitions (Hematology, Biochemistry, etc.).
    Ordered by display_order then name. Name is unique within the tenant.

ExamDefinition
    Reusable template for one type of exam: code, sample type, turnaround time.
    Code is unique within the tenant and is immutable once referenced by an
    exam item. Hard delete is blocked — use deactivation.

LabExamSettings
    Per-lab customisation of an exam definition: reference range, turnaround
    override, enabled flag, internal notes. One record per exam per tenant
    (enforced via OneToOneField). Created/replaced atomically via PUT.

PricingRule
    Contextual pricing rule targeting an exam definition, optionally scoped to a
    partner organization and/or source type. Rules are resolved by specificity at
    request item creation time. Supports fixed prices and percentage discounts.
"""
import uuid
from django.db import models
from django.utils import timezone

from common.models import BaseModel


class SampleType(models.TextChoices):
    BLOOD = 'BLOOD', 'Blood'
    URINE = 'URINE', 'Urine'
    STOOL = 'STOOL', 'Stool'
    CSF = 'CSF', 'Cerebrospinal Fluid'
    SWAB = 'SWAB', 'Swab'
    SALIVA = 'SALIVA', 'Saliva'
    TISSUE = 'TISSUE', 'Tissue'
    OTHER = 'OTHER', 'Other'


class ExamCategory(BaseModel):
    """
    Thematic grouping for exam definitions.
    Ordering is controlled by display_order (ascending), then name.
    """
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True, default='')
    display_order = models.IntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Exam Category'
        verbose_name_plural = 'Exam Categories'
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class ExamDefinition(BaseModel):
    """
    Reusable descriptor for one exam type.

    `code` is a short identifier used in external reporting and integrations.
    It is unique within the tenant and must not change once an exam item
    references this definition (enforced at service layer).

    Hard delete is blocked — deactivate instead. The PROTECT on the category FK
    prevents deleting a category that has exams.
    """
    category = models.ForeignKey(
        ExamCategory,
        on_delete=models.PROTECT,
        related_name='exams',
    )
    code = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    sample_type = models.CharField(
        max_length=10,
        choices=SampleType.choices,
        db_index=True,
    )
    turnaround_hours = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Expected hours from sample receipt to result. Overridable per lab via LabExamSettings.',
    )
    description = models.TextField(blank=True, default='')
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=0,
        help_text='Reference/default catalog price for this exam.',
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Exam Definition'
        verbose_name_plural = 'Exam Definitions'
        ordering = ['category__display_order', 'name']

    def __str__(self):
        return f'[{self.code}] {self.name}'

    def delete(self, *args, **kwargs):
        """Block hard delete. Service layer enforces deactivation-only via API."""
        raise PermissionError(
            'Exam definitions cannot be deleted. Use deactivation instead.'
        )


class LabExamSettings(models.Model):
    """
    Per-tenant customisation of an exam definition.

    Acts as an extension record: the base definition holds canonical values;
    this record holds lab-specific overrides. Created via PUT (upsert semantics).
    No created_at — updated_at captures the last modification timestamp.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exam_definition = models.OneToOneField(
        ExamDefinition,
        on_delete=models.CASCADE,
        related_name='lab_settings',
    )
    reference_range = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Lab-specific normal range, e.g. "3.5–5.0 mmol/L".',
    )
    turnaround_hours_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Overrides exam_definition.turnaround_hours for this lab.',
    )
    is_enabled = models.BooleanField(
        default=True,
        db_index=True,
        help_text='Set to False to hide this exam from this lab\'s ordering workflow.',
    )
    internal_notes = models.TextField(blank=True, default='')
    updated_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='updated_lab_settings',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Lab Exam Settings'
        verbose_name_plural = 'Lab Exam Settings'

    def __str__(self):
        return f'Settings for {self.exam_definition}'


class PricingType(models.TextChoices):
    FIXED_PRICE = 'FIXED_PRICE', 'Fixed Price'
    PERCENTAGE_DISCOUNT = 'PERCENTAGE_DISCOUNT', 'Percentage Discount'


class PricingRule(BaseModel):
    """
    Contextual pricing rule for an exam definition.

    Rules are matched by specificity when resolving the billed price for a
    request item:
        1. exam + partner_organization  (most specific)
        2. exam + source_type
        3. exam only  (broadest)

    Within the same specificity level, higher ``priority`` wins, then most
    recently created as tiebreaker.

    ``pricing_type`` determines how ``value`` is interpreted:
        - FIXED_PRICE: ``value`` is the absolute billed price.
        - PERCENTAGE_DISCOUNT: ``value`` is a percentage off the exam unit_price.

    Optional date bounds (``start_date`` / ``end_date``) restrict the rule's
    active period. Both default to NULL (always active). ``is_active`` provides
    a quick on/off toggle.
    """
    exam_definition = models.ForeignKey(
        ExamDefinition,
        on_delete=models.PROTECT,
        related_name='pricing_rules',
    )
    partner_organization = models.ForeignKey(
        'partners.PartnerOrganization',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='pricing_rules',
        help_text='Set to target a specific partner. NULL = not partner-specific.',
    )
    source_type = models.CharField(
        max_length=25,
        blank=True,
        default='',
        help_text='DIRECT_PATIENT or PARTNER_ORGANIZATION. Empty = any source type.',
    )
    pricing_type = models.CharField(
        max_length=25,
        choices=PricingType.choices,
        default=PricingType.FIXED_PRICE,
    )
    value = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text=(
            'For FIXED_PRICE: the absolute billed price. '
            'For PERCENTAGE_DISCOUNT: the discount percentage (e.g. 10 = 10%% off).'
        ),
    )
    priority = models.IntegerField(
        default=0,
        help_text='Higher value = higher priority within the same specificity level.',
    )
    is_active = models.BooleanField(default=True, db_index=True)
    start_date = models.DateField(
        null=True, blank=True,
        help_text='Rule is active from this date (inclusive). NULL = no lower bound.',
    )
    end_date = models.DateField(
        null=True, blank=True,
        help_text='Rule is active until this date (inclusive). NULL = no upper bound.',
    )
    notes = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_pricing_rules',
    )

    class Meta:
        verbose_name = 'Pricing Rule'
        verbose_name_plural = 'Pricing Rules'
        ordering = ['-priority', '-created_at']
        indexes = [
            models.Index(fields=['exam_definition', 'is_active']),
            models.Index(fields=['partner_organization', 'is_active']),
        ]

    def __str__(self):
        parts = [self.exam_definition.code]
        if self.partner_organization_id:
            parts.append(f'partner={self.partner_organization_id}')
        if self.source_type:
            parts.append(f'source={self.source_type}')
        parts.append(f'{self.pricing_type}={self.value}')
        return ' | '.join(parts)

    def clean(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise DjangoValidationError(
                {'end_date': 'end_date must be on or after start_date.'}
            )
