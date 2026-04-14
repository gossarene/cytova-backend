"""
Cytova — Lab Settings

Per-tenant singleton that stores:
- Laboratory identity (name, subtitle, logo, address, contact, signature)
- Report display options (what sections appear on generated PDF reports)

One ``LabSettings`` row exists per tenant schema. A get-or-create helper
returns the current row on first access so the app never has to check
for existence at call sites.
"""
from django.db import models

from common.models import BaseModel


class LabSettings(BaseModel):
    """Singleton per tenant. Use ``LabSettings.get_solo()`` to fetch."""

    # -- Laboratory identity --
    lab_name = models.CharField(max_length=255, blank=True, default='')
    lab_subtitle = models.CharField(max_length=255, blank=True, default='',
                                     help_text='e.g. "Medical Analysis Laboratory"')
    logo_file_key = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Internal storage key for the uploaded laboratory logo image.',
    )
    logo_url = models.URLField(
        blank=True, default='',
        help_text='External URL fallback when no file has been uploaded (display only).',
    )
    address = models.TextField(blank=True, default='')
    phone = models.CharField(max_length=50, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    website = models.CharField(max_length=255, blank=True, default='')
    signature_file_key = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Internal storage key for the validator signature/stamp image.',
    )
    legal_footer = models.TextField(
        blank=True, default='',
        help_text='Confidentiality / legal text printed at the bottom of reports.',
    )

    # -- Report display options --
    show_logo = models.BooleanField(default=True)
    show_lab_address = models.BooleanField(default=True)
    show_prescriber = models.BooleanField(default=True)
    show_collection_datetime = models.BooleanField(default=True)
    show_patient_age = models.BooleanField(default=True)
    show_patient_sex = models.BooleanField(default=True)
    show_exam_technique = models.BooleanField(default=True)
    show_reference_ranges = models.BooleanField(default=True)
    show_patient_comments = models.BooleanField(default=True)
    show_final_conclusion = models.BooleanField(default=True)
    show_signature = models.BooleanField(default=True)
    show_legal_footer = models.BooleanField(default=True)
    show_abnormal_flags = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'Lab Settings'
        verbose_name_plural = 'Lab Settings'

    def __str__(self):
        return self.lab_name or '(Lab settings)'

    @classmethod
    def get_solo(cls) -> 'LabSettings':
        """Return the single tenant-scoped settings row, creating it if missing."""
        obj, _ = cls.objects.get_or_create()
        return obj
