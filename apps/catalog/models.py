"""
Cytova — Catalog Models

ExamFamily
    Primary classification of exam definitions (Hematology, Biochemistry, etc.).
    Replaces the former ExamCategory concept with lab-standard terminology.

ExamSubFamily
    Optional secondary classification within a family.

TubeType
    Specimen collection tube type (EDTA, Citrate, Dry, etc.).

ExamTechnique
    Laboratory technique used to perform the analysis (PCR, Immunoassay, etc.).

ExamDefinition
    Reusable template for one type of exam: code, sample type, family,
    tube type, technique, fasting requirement, turnaround time.
    Code is unique within the tenant and is immutable once referenced by an
    exam item. Hard delete is blocked — use deactivation.

LabExamSettings
    Per-lab customisation of an exam definition.

PricingRule
    Contextual pricing rule targeting an exam definition.
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


# ---------------------------------------------------------------------------
# Reference models (lookup tables)
# ---------------------------------------------------------------------------

class ExamFamily(BaseModel):
    """Primary exam classification (replaces ExamCategory)."""
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True, default='')
    display_order = models.IntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Exam Family'
        verbose_name_plural = 'Exam Families'
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class ExamSubFamily(BaseModel):
    """Optional secondary classification within a family."""
    family = models.ForeignKey(
        ExamFamily,
        on_delete=models.CASCADE,
        related_name='sub_families',
    )
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Exam Sub-Family'
        verbose_name_plural = 'Exam Sub-Families'
        ordering = ['family__display_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['family', 'name'], name='unique_subfamily_per_family'),
        ]

    def __str__(self):
        return f'{self.family.name} > {self.name}'


class TubeType(BaseModel):
    """Specimen collection tube type."""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Tube Type'
        verbose_name_plural = 'Tube Types'
        ordering = ['name']

    def __str__(self):
        return self.name


class ExamTechnique(BaseModel):
    """Laboratory technique used to perform an analysis."""
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Exam Technique'
        verbose_name_plural = 'Exam Techniques'
        ordering = ['name']

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Legacy model kept for backward compatibility during migration
# ---------------------------------------------------------------------------

class ExamCategory(BaseModel):
    """
    DEPRECATED — replaced by ExamFamily.
    Kept temporarily so existing migrations and FK references remain valid
    until full data migration is complete.
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


# ---------------------------------------------------------------------------
# ExamDefinition
# ---------------------------------------------------------------------------

class ExamDefinition(BaseModel):
    """
    Reusable descriptor for one exam type.

    `code` is a short identifier used in external reporting and integrations.
    It is unique within the tenant and must not change once an exam item
    references this definition.

    Hard delete is blocked — deactivate instead.
    """
    # Legacy FK — kept until data migration removes it
    category = models.ForeignKey(
        ExamCategory,
        on_delete=models.PROTECT,
        related_name='exams',
        null=True,
        blank=True,
    )

    # New structured classification
    family = models.ForeignKey(
        ExamFamily,
        on_delete=models.PROTECT,
        related_name='exam_definitions',
        null=True,
        blank=True,
    )
    sub_family = models.ForeignKey(
        ExamSubFamily,
        on_delete=models.SET_NULL,
        related_name='exams',
        null=True,
        blank=True,
    )
    tube_type = models.ForeignKey(
        TubeType,
        on_delete=models.SET_NULL,
        related_name='exams',
        null=True,
        blank=True,
    )
    technique = models.ForeignKey(
        ExamTechnique,
        on_delete=models.SET_NULL,
        related_name='exams',
        null=True,
        blank=True,
    )
    fasting_required = models.BooleanField(
        default=False,
        help_text='Whether the patient must fast before specimen collection.',
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
        help_text='Expected hours from sample receipt to result.',
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
        ordering = ['family__display_order', 'name']

    def __str__(self):
        return f'[{self.code}] {self.name}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Exam definitions cannot be deleted. Use deactivation instead.'
        )


# ---------------------------------------------------------------------------
# LabExamSettings (unchanged)
# ---------------------------------------------------------------------------

class LabExamSettings(models.Model):
    """Per-tenant customisation of an exam definition."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exam_definition = models.OneToOneField(
        ExamDefinition,
        on_delete=models.CASCADE,
        related_name='lab_settings',
    )
    reference_range = models.CharField(max_length=100, blank=True, default='')
    turnaround_hours_override = models.PositiveIntegerField(null=True, blank=True)
    is_enabled = models.BooleanField(default=True, db_index=True)
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


# ---------------------------------------------------------------------------
# PricingRule (unchanged)
# ---------------------------------------------------------------------------

class PricingType(models.TextChoices):
    FIXED_PRICE = 'FIXED_PRICE', 'Fixed Price'
    PERCENTAGE_DISCOUNT = 'PERCENTAGE_DISCOUNT', 'Percentage Discount'


class PricingRule(BaseModel):
    """Contextual pricing rule for an exam definition."""
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
    )
    source_type = models.CharField(max_length=25, blank=True, default='')
    pricing_type = models.CharField(
        max_length=25,
        choices=PricingType.choices,
        default=PricingType.FIXED_PRICE,
    )
    value = models.DecimalField(max_digits=12, decimal_places=4)
    priority = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
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
