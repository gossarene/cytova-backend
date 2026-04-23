"""
Cytova — Label Printing Presets

Platform-managed, reusable label layout templates. Lives in the public
schema so a single catalog is shared across every tenant — Cytova
platform administrators curate the list, tenants pick from it.

A preset is intentionally a *template*: when a laboratory selects one,
its values are COPIED into ``LabSettings`` (see ``apps.lab_settings``).
The actual rendering always reads the lab's frozen effective config,
never a live preset row. This keeps already-printed layouts stable
even if a platform admin later tweaks a preset.
"""
import uuid

from django.db import models


class LabelPrintMode(models.TextChoices):
    A4_SHEET = 'A4_SHEET', 'A4 Multi-Label Sheet'
    THERMAL_ROLL = 'THERMAL_ROLL', 'Thermal Roll'


class LabelPrintPreset(models.Model):
    """
    Reusable label layout template. Managed centrally by Cytova platform
    admins (no tenant-facing write endpoints yet — this model is the
    backend foundation for a future Cytova Admin UI).

    ``is_system`` marks presets seeded via data migration. They cannot
    be deleted through normal flows; their dimensions represent the
    Cytova "factory defaults" for each print mode.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=100)
    code = models.CharField(
        max_length=50,
        unique=True,
        help_text='Stable machine identifier, e.g. SYS_A4_10_LABELS.',
    )
    print_mode = models.CharField(
        max_length=20,
        choices=LabelPrintMode.choices,
        db_index=True,
    )

    # Page dimensions — in A4_SHEET mode these describe the paper; in
    # THERMAL_ROLL mode they describe the physical label stock page
    # (usually equal to label_width_mm × label_height_mm).
    page_width_mm = models.PositiveSmallIntegerField()
    page_height_mm = models.PositiveSmallIntegerField()

    label_width_mm = models.PositiveSmallIntegerField()
    label_height_mm = models.PositiveSmallIntegerField()

    margin_top_mm = models.PositiveSmallIntegerField(default=0)
    margin_left_mm = models.PositiveSmallIntegerField(default=0)
    horizontal_gap_mm = models.PositiveSmallIntegerField(default=0)
    vertical_gap_mm = models.PositiveSmallIntegerField(default=0)
    thermal_gap_mm = models.PositiveSmallIntegerField(default=0)

    show_barcode = models.BooleanField(default=True)
    show_numeric_code = models.BooleanField(default=True)

    is_active = models.BooleanField(default=True, db_index=True)
    is_system = models.BooleanField(
        default=False,
        help_text='True for platform-seeded factory presets.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Label Print Preset'
        verbose_name_plural = 'Label Print Presets'
        ordering = ['print_mode', 'name']

    def __str__(self):
        return f'{self.name} ({self.code})'

    def to_effective_config(self) -> dict:
        """
        Return a dict of the layout values that ``LabSettings`` copies
        when a laboratory selects this preset. Keys match the
        ``label_*`` columns on ``LabSettings`` minus the ``label_``
        prefix for convenience — callers prefix as needed.
        """
        return {
            'print_mode': self.print_mode,
            'page_width_mm': self.page_width_mm,
            'page_height_mm': self.page_height_mm,
            'label_width_mm': self.label_width_mm,
            'label_height_mm': self.label_height_mm,
            'margin_top_mm': self.margin_top_mm,
            'margin_left_mm': self.margin_left_mm,
            'horizontal_gap_mm': self.horizontal_gap_mm,
            'vertical_gap_mm': self.vertical_gap_mm,
            'thermal_gap_mm': self.thermal_gap_mm,
            'show_barcode': self.show_barcode,
            'show_numeric_code': self.show_numeric_code,
        }
