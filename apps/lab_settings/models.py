"""
Cytova — Lab Settings

Per-tenant singleton that stores:
- Laboratory identity (name, subtitle, logo, address, contact, signature)
- Report display options (what sections appear on generated PDF reports)

One ``LabSettings`` row exists per tenant schema. A get-or-create helper
returns the current row on first access so the app never has to check
for existence at call sites.
"""
from django.core.validators import MinValueValidator
from django.db import models

from common.models import BaseModel


class LabelNumberingMode(models.TextChoices):
    """How a request's labels share (or not) a numeric label code.

    - ``PER_FAMILY`` — every label gets its own freshly-allocated
      numeric code from the tenant sequence. This is the historical
      behaviour and stays the default.
    - ``SAME_REQUEST_NUMBER`` — one numeric code is allocated per
      request and reused for every label in the batch. Useful when
      operators want a single visible identifier across all tubes
      from one request, with the family disambiguating them.
    """
    PER_FAMILY = 'PER_FAMILY', 'Per family'
    SAME_REQUEST_NUMBER = 'SAME_REQUEST_NUMBER', 'Same request number'


class LabelSequenceResetPeriod(models.TextChoices):
    """How often the tenant's label sequence counter resets.

    - ``MONTHLY`` — historical behaviour, sequence resets at the
      first of every month. Period key is ``YYYY-MM``.
    - ``YEARLY`` — sequence resets at the first of every year.
      Period key is ``YYYY``. Useful for labs with low monthly
      throughput that prefer one continuous number line per year.
    """
    MONTHLY = 'MONTHLY', 'Monthly'
    YEARLY = 'YEARLY', 'Yearly'


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

    # -- Logo rendering on reports --
    logo_position = models.CharField(
        max_length=10,
        choices=[('LEFT', 'Left'), ('CENTER', 'Center'), ('RIGHT', 'Right')],
        default='RIGHT',
    )
    logo_max_width_mm = models.PositiveSmallIntegerField(
        default=40,
        help_text='Maximum width of the logo bounding box in mm.',
    )
    logo_max_height_mm = models.PositiveSmallIntegerField(
        default=20,
        help_text='Maximum height of the logo bounding box in mm.',
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

    # -- Result PDF protection --
    lab_secret_code = models.CharField(
        max_length=2,
        blank=True,
        default='',
        help_text='2-character code auto-generated per lab. Used as a suffix '
                  'in PDF password derivation. Regenerable by lab admin.',
    )
    result_pdf_password_enabled = models.BooleanField(
        default=False,
        help_text='When enabled, generated result PDFs require a password to open.',
    )
    result_pdf_password_mode = models.CharField(
        max_length=30,
        choices=[
            ('PATIENT_DOB', 'Patient date of birth (YYYYMMDD)'),
            ('PATIENT_PHONE', 'Patient phone (digits only)'),
            ('REQUEST_REFERENCE', 'Request public reference'),
            ('DOB_PLUS_PHONE_SUFFIX', 'DOB + last 4 digits of phone'),
            ('DOB_PHONE_SECRET', 'DOB + phone suffix + lab secret code'),
        ],
        default='DOB_PHONE_SECRET',
    )
    result_pdf_password_hint = models.CharField(
        max_length=255,
        blank=True,
        default='Your date of birth (YYYYMMDD) followed by the last 4 digits of your phone number.',
        help_text='Hint text displayed to users who need to open the PDF.',
    )

    # -- Patient notification channels --
    notification_enable_secure_link = models.BooleanField(
        default=True,
        help_text='Allow generating secure access links for patient results.',
    )
    notification_enable_whatsapp_share = models.BooleanField(
        default=True,
        help_text='Show WhatsApp share option for patient result links.',
    )
    notification_enable_email = models.BooleanField(
        default=False,
        help_text='Enable email notification to patients (not yet implemented).',
    )
    notification_enable_sms = models.BooleanField(
        default=False,
        help_text='Enable SMS notification to patients (not yet implemented).',
    )
    notification_enable_cytova = models.BooleanField(
        default=True,
        help_text='Allow sharing patient results into the global Cytova '
                  'patient portal (Notify Cytova). When disabled, the '
                  'endpoint refuses with CYTOVA_CHANNEL_DISABLED and the '
                  'lab UI hides the channel.',
    )

    # -- Patient notification email templates (Phase 1 of the
    #    customisable-templates rollout) --
    #
    # Both fields are operator-customisable copy that the patient-
    # result-ready email service renders via
    # ``common.email.safe_template.render_safe_notification_template``
    # at send time. The renderer recognises only the four placeholders
    # listed in ``PATIENT_NOTIFICATION_ALLOWED_VARS``; the serializer
    # validator refuses to save any template that references anything
    # else. That two-layer guarantee is the load-bearing safety
    # property — operators cannot smuggle medical content into a
    # patient email no matter what they paste into the field.
    #
    # The defaults reproduce today's hard-coded copy verbatim, so
    # tenants that touch nothing experience zero behavioural drift
    # when Phase 2 wires the templates into the email service.
    patient_result_email_subject_template = models.CharField(
        max_length=200,
        blank=True,
        default='Your lab result is ready',
        help_text='Subject line of the patient result-ready email. '
                  'Allowed variables: {{ patient_first_name }}, '
                  '{{ lab_name }}, {{ result_link }}, '
                  '{{ request_reference }}.',
    )
    patient_result_email_body_template = models.TextField(
        blank=True,
        default=(
            'Hello {{ patient_first_name }},\n\n'
            'Your lab result is ready. You can access it securely '
            'using the link below:\n\n'
            '{{ result_link }}\n\n'
            'For your privacy, please do not share this link.'
        ),
        help_text='Body of the patient result-ready email. Same '
                  'allowed variables as the subject. Operators MUST '
                  'NOT include medical content (result values, '
                  'diagnosis, exam names) — the validator refuses '
                  'any template referencing forbidden placeholders.',
    )

    # -- Label generation behaviour --
    # New in Phase 1 of the flexible-labels rollout. Defaults are
    # chosen to preserve the pre-rollout behaviour exactly:
    #   - ``label_numbering_mode = PER_FAMILY``  → one fresh numeric
    #     code per label, identical to today's allocator loop.
    #   - ``extra_label_count = 2``             → matches the
    #     hard-coded ``EXTRA_LABELS_BONUS = 2`` constant the label
    #     service has used since launch.
    #   - ``label_sequence_reset_period = MONTHLY`` → the
    #     ``LabelSequence`` model is currently keyed on ``(year,
    #     month)``, which is monthly reset by definition.
    # The label generation service does NOT yet read these fields —
    # Phases 2-4 wire them up. Surfacing them here first lets the
    # admin UI render the controls and lets ops review per-tenant
    # values via the read serializer ahead of the behaviour change.
    label_numbering_mode = models.CharField(
        max_length=24, choices=LabelNumberingMode.choices,
        default=LabelNumberingMode.PER_FAMILY,
        help_text='How a request\'s labels share (or not) a numeric '
                  'label code. Defaults to PER_FAMILY (current '
                  'behaviour).',
    )
    extra_label_count = models.PositiveSmallIntegerField(
        default=2,
        validators=[MinValueValidator(0)],
        help_text='Number of extra labels appended to every batch on '
                  'top of the per-family labels. Default 2 preserves '
                  'the pre-rollout behaviour; set to 0 to disable.',
    )
    label_sequence_reset_period = models.CharField(
        max_length=10, choices=LabelSequenceResetPeriod.choices,
        default=LabelSequenceResetPeriod.MONTHLY,
        help_text='How often the tenant\'s label sequence counter '
                  'resets. Defaults to MONTHLY (current behaviour).',
    )

    # -- Billing --
    financial_document_mode = models.CharField(
        max_length=30,
        choices=[
            ('INVOICE_ONLY', 'Invoice only'),
            ('STATEMENT_ONLY', 'Financial statement only'),
            ('BOTH', 'Both invoice and financial statement'),
        ],
        default='INVOICE_ONLY',
        help_text='Controls which financial document types this lab can generate.',
    )
    default_invoice_vat_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Default VAT percentage applied on invoices (e.g. 18.00 '
                  'for 18%). Snapshotted at invoice generation time.',
    )

    # -- Report appearance (controlled) --
    report_accent_color = models.CharField(
        max_length=7,
        blank=True,
        default='#0f172a',
        help_text='Hex color for family section titles and accent lines '
                  '(e.g. "#0f172a"). Must be a valid 7-char hex code.',
    )
    show_family_divider_line = models.BooleanField(
        default=True,
        help_text='Draw a thin horizontal line below each exam family title.',
    )
    show_previous_results = models.BooleanField(
        default=True,
        help_text='Include the previous result column in report tables.',
    )

    # -- Label printing: effective config --
    # When a preset is selected via the API, its values are copied
    # into the fields below. Rendering reads exclusively from these
    # frozen fields — never from the preset row — so platform admins
    # can revise presets without silently reformatting labels that a
    # laboratory has already validated operationally.
    label_print_mode = models.CharField(
        max_length=20,
        choices=[('A4_SHEET', 'A4 Multi-Label Sheet'),
                 ('THERMAL_ROLL', 'Thermal Roll')],
        default='A4_SHEET',
    )
    label_preset = models.ForeignKey(
        'labels.LabelPrintPreset',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        help_text='Last preset applied. Kept as an audit reference only — '
                  'effective values live on this row and are authoritative.',
    )
    label_page_width_mm = models.PositiveSmallIntegerField(default=210)
    label_page_height_mm = models.PositiveSmallIntegerField(default=297)
    label_label_width_mm = models.PositiveSmallIntegerField(default=90)
    label_label_height_mm = models.PositiveSmallIntegerField(default=50)
    label_margin_top_mm = models.PositiveSmallIntegerField(default=15)
    label_margin_left_mm = models.PositiveSmallIntegerField(default=10)
    label_horizontal_gap_mm = models.PositiveSmallIntegerField(default=5)
    label_vertical_gap_mm = models.PositiveSmallIntegerField(default=5)
    label_thermal_gap_mm = models.PositiveSmallIntegerField(default=2)
    label_show_barcode = models.BooleanField(default=True)
    label_show_numeric_code = models.BooleanField(default=True)

    # -- Internal-workflow notifications -----------------------------------
    # Three-level kill-switch for the staff-to-staff email surface
    # (review-ready + rejection emails delivered through
    # ``apps.internal_notifications``):
    #
    #   - The global flag is the master off-switch. When False,
    #     NO workflow email is sent regardless of per-event flags
    #     or per-user preferences.
    #   - The two per-event flags suppress one channel without
    #     affecting the other (a lab can keep rejection emails on
    #     while muting review-ready blasts during a busy day).
    #   - The actual recipient list is filtered by the per-user
    #     ``StaffUser.receive_*`` flags, so role-based defaults
    #     are smart suggestions, not the final authority.
    #
    # The defaults preserve the prior behaviour: a lab that does
    # not visit the new settings page keeps receiving the same
    # emails the role-based resolver delivered before this phase.
    internal_notifications_enabled = models.BooleanField(
        default=True,
        help_text='Master switch for internal-workflow emails '
                  '(biologist review-ready, technician rejection). '
                  'When False, no workflow email is sent regardless '
                  'of the per-event flags below.',
    )
    notify_review_ready_enabled = models.BooleanField(
        default=True,
        help_text='If False, biologist review-ready emails are '
                  'suppressed even when the master switch is on.',
    )
    notify_result_rejected_enabled = models.BooleanField(
        default=True,
        help_text='If False, technician rejection emails are '
                  'suppressed even when the master switch is on.',
    )

    class Meta:
        verbose_name = 'Lab Settings'
        verbose_name_plural = 'Lab Settings'

    def __str__(self):
        return self.lab_name or '(Lab settings)'

    @classmethod
    def get_solo(cls) -> 'LabSettings':
        """Return the single tenant-scoped settings row, creating it if missing."""
        obj, created = cls.objects.get_or_create()
        if not obj.lab_secret_code:
            obj.lab_secret_code = cls._generate_secret_code()
            obj.save(update_fields=['lab_secret_code', 'updated_at'])
        return obj

    @staticmethod
    def _generate_secret_code() -> str:
        """Generate a 2-char uppercase alphanumeric code, avoiding ambiguous chars."""
        import secrets
        alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
        return ''.join(secrets.choice(alphabet) for _ in range(2))

    # -- Label-config helpers --------------------------------------------------
    LABEL_CONFIG_FIELDS = (
        'label_page_width_mm', 'label_page_height_mm',
        'label_label_width_mm', 'label_label_height_mm',
        'label_margin_top_mm', 'label_margin_left_mm',
        'label_horizontal_gap_mm', 'label_vertical_gap_mm',
        'label_thermal_gap_mm',
        'label_show_barcode', 'label_show_numeric_code',
    )

    def apply_preset(self, preset) -> None:
        """
        Copy a ``LabelPrintPreset``'s values into the effective config.
        The caller is responsible for calling ``save()``.
        """
        values = preset.to_effective_config()
        self.label_print_mode = values['print_mode']
        self.label_page_width_mm = values['page_width_mm']
        self.label_page_height_mm = values['page_height_mm']
        self.label_label_width_mm = values['label_width_mm']
        self.label_label_height_mm = values['label_height_mm']
        self.label_margin_top_mm = values['margin_top_mm']
        self.label_margin_left_mm = values['margin_left_mm']
        self.label_horizontal_gap_mm = values['horizontal_gap_mm']
        self.label_vertical_gap_mm = values['vertical_gap_mm']
        self.label_thermal_gap_mm = values['thermal_gap_mm']
        self.label_show_barcode = values['show_barcode']
        self.label_show_numeric_code = values['show_numeric_code']
        self.label_preset = preset

    def to_label_layout_config(self):
        """Return a ``LabelLayoutConfig`` ready for the rendering engine."""
        from apps.labels.renderers import LabelLayoutConfig
        return LabelLayoutConfig(
            print_mode=self.label_print_mode,
            page_width_mm=self.label_page_width_mm,
            page_height_mm=self.label_page_height_mm,
            label_width_mm=self.label_label_width_mm,
            label_height_mm=self.label_label_height_mm,
            margin_top_mm=self.label_margin_top_mm,
            margin_left_mm=self.label_margin_left_mm,
            horizontal_gap_mm=self.label_horizontal_gap_mm,
            vertical_gap_mm=self.label_vertical_gap_mm,
            thermal_gap_mm=self.label_thermal_gap_mm,
            show_barcode=self.label_show_barcode,
            show_numeric_code=self.label_show_numeric_code,
        )
