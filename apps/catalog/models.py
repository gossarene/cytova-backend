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
    Time-bound pricing attached to an exam definition. Immutable after creation.
    To revise a price: close the current rule (set effective_to) and add a new one.
    No two active rules for the same exam may have overlapping date ranges.
    Prices are snapshotted onto exam_items at request confirmation.
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


class PricingRule(models.Model):
    """
    Time-bound pricing for an exam definition.

    Immutable after creation — prices are frozen because they are snapshotted
    onto exam_items at request confirmation. To revise: close the active rule
    (POST /pricing/{id}/close/) and create a new one.

    Application-layer validation ensures no two rules for the same exam overlap.
    CHECK: effective_to IS NULL OR effective_to > effective_from.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exam_definition = models.ForeignKey(
        ExamDefinition,
        on_delete=models.PROTECT,
        related_name='pricing_rules',
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=4)
    billed_price = models.DecimalField(max_digits=12, decimal_places=4)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    insurance_code = models.CharField(max_length=50, blank=True, default='')
    created_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_pricing_rules',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Pricing Rule'
        verbose_name_plural = 'Pricing Rules'
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['exam_definition', 'effective_from']),
        ]

    def __str__(self):
        end = str(self.effective_to) if self.effective_to else 'open'
        return f'{self.exam_definition.code} {self.effective_from}→{end}'

    def save(self, *args, **kwargs):
        """Immutable after creation."""
        if not self._state.adding:
            raise PermissionError('Pricing rules are immutable. Close the current rule and create a new one.')
        super().save(*args, **kwargs)
