from django.conf import settings as dj_settings
from rest_framework import serializers

from apps.labels.models import LabelPrintPreset
from common.email.safe_template import (
    PATIENT_NOTIFICATION_ALLOWED_VARS,
    find_disallowed_variables,
)
from .models import LabSettings


_ALLOWED_LOGO_MIMES = frozenset({
    'image/png', 'image/jpeg', 'image/gif', 'image/svg+xml',
})
_MAX_LOGO_SIZE = 2 * 1024 * 1024  # 2 MB


class LabSettingsSerializer(serializers.ModelSerializer):
    has_logo_file = serializers.SerializerMethodField()
    logo_preview_url = serializers.SerializerMethodField()

    class Meta:
        model = LabSettings
        fields = [
            # identity
            'lab_name', 'lab_subtitle',
            'logo_file_key', 'logo_url',
            'has_logo_file', 'logo_preview_url',
            'address', 'phone', 'email', 'website',
            'legal_footer',
            # display options
            'show_logo', 'show_lab_address', 'show_prescriber',
            'show_collection_datetime', 'show_patient_age', 'show_patient_sex',
            'show_exam_technique', 'show_reference_ranges',
            'show_patient_comments', 'show_final_conclusion',
            'show_signature', 'show_legal_footer', 'show_abnormal_flags',
            # logo rendering
            'logo_position', 'logo_max_width_mm', 'logo_max_height_mm',
            # report appearance
            'report_accent_color', 'show_family_divider_line',
            'show_previous_results',
            # billing
            # PDF protection
            'lab_secret_code',
            'result_pdf_password_enabled', 'result_pdf_password_mode',
            'result_pdf_password_hint',
            # notification channels
            'notification_enable_secure_link',
            'notification_enable_whatsapp_share',
            'notification_enable_email',
            'notification_enable_sms',
            'notification_enable_cytova',
            # patient-result email templates (operator-customisable
            # subject + body, allow-list-validated). The renderer
            # consumes these in Phase 2 of the rollout — Phase 1
            # only adds the storage + validator surface.
            'patient_result_email_subject_template',
            'patient_result_email_body_template',
            # label generation behaviour (Phase 1; not yet wired into
            # the label service — see LabSettings model docstring).
            'label_numbering_mode',
            'extra_label_count',
            'label_sequence_reset_period',
            # billing
            'financial_document_mode', 'default_invoice_vat_rate',
            # label printing
            'label_print_mode', 'label_preset',
            'label_page_width_mm', 'label_page_height_mm',
            'label_label_width_mm', 'label_label_height_mm',
            'label_margin_top_mm', 'label_margin_left_mm',
            'label_horizontal_gap_mm', 'label_vertical_gap_mm',
            'label_thermal_gap_mm',
            'label_show_barcode', 'label_show_numeric_code',
            # internal-workflow notification settings (staff-to-staff
            # workflow emails — biologist review-ready, technician
            # rejection). Distinct from the patient-facing
            # ``notification_enable_*`` block above.
            'internal_notifications_enabled',
            'notify_review_ready_enabled',
            'notify_result_rejected_enabled',
            'updated_at',
        ]
        read_only_fields = [
            'updated_at', 'logo_file_key',  # populated by upload endpoint
            'has_logo_file', 'logo_preview_url',
        ]

    def get_has_logo_file(self, obj):
        return bool(obj.logo_file_key)

    def get_logo_preview_url(self, obj):
        if obj.logo_file_key:
            return '/lab-settings/logo/'
        return None


class LabSettingsUpdateSerializer(serializers.ModelSerializer):
    """
    Patch serializer. Special rule: when ``label_preset`` is supplied, the
    preset's layout values are copied verbatim into the effective label
    config fields — this is the contract that makes rendering stable
    against platform-side preset edits (see ``LabSettings.apply_preset``).
    """

    label_preset = serializers.PrimaryKeyRelatedField(
        queryset=LabelPrintPreset.objects.filter(is_active=True),
        required=False, allow_null=True,
    )

    class Meta:
        model = LabSettings
        fields = [
            'lab_name', 'lab_subtitle', 'logo_url',
            'address', 'phone', 'email', 'website',
            'legal_footer',
            'show_logo', 'show_lab_address', 'show_prescriber',
            'show_collection_datetime', 'show_patient_age', 'show_patient_sex',
            'show_exam_technique', 'show_reference_ranges',
            'show_patient_comments', 'show_final_conclusion',
            'show_signature', 'show_legal_footer', 'show_abnormal_flags',
            # logo rendering
            'logo_position', 'logo_max_width_mm', 'logo_max_height_mm',
            # report appearance
            'report_accent_color', 'show_family_divider_line',
            'show_previous_results',
            # billing
            # PDF protection
            'lab_secret_code',
            'result_pdf_password_enabled', 'result_pdf_password_mode',
            'result_pdf_password_hint',
            # notification channels
            'notification_enable_secure_link',
            'notification_enable_whatsapp_share',
            'notification_enable_email',
            'notification_enable_sms',
            'notification_enable_cytova',
            # patient-result email templates (operator-customisable
            # subject + body, allow-list-validated). The renderer
            # consumes these in Phase 2 of the rollout — Phase 1
            # only adds the storage + validator surface.
            'patient_result_email_subject_template',
            'patient_result_email_body_template',
            # label generation behaviour (Phase 1; not yet wired into
            # the label service — see LabSettings model docstring).
            'label_numbering_mode',
            'extra_label_count',
            'label_sequence_reset_period',
            # billing
            'financial_document_mode', 'default_invoice_vat_rate',
            # label printing — explicit values allowed for fine-tuning,
            # or pass `label_preset` to copy a full template in one shot.
            'label_print_mode', 'label_preset',
            'label_page_width_mm', 'label_page_height_mm',
            'label_label_width_mm', 'label_label_height_mm',
            'label_margin_top_mm', 'label_margin_left_mm',
            'label_horizontal_gap_mm', 'label_vertical_gap_mm',
            'label_thermal_gap_mm',
            'label_show_barcode', 'label_show_numeric_code',
            # internal-workflow notification settings — LAB_ADMIN-
            # writable via the same PATCH endpoint that already
            # owns the rest of the lab settings.
            'internal_notifications_enabled',
            'notify_review_ready_enabled',
            'notify_result_rejected_enabled',
        ]
        extra_kwargs = {f: {'required': False} for f in fields}

    def update(self, instance, validated_data):
        preset = validated_data.pop('label_preset', serializers.empty)
        # Apply preset first; any explicit fields in the same payload
        # then override (allowing "pick preset and tweak one value").
        if preset is not serializers.empty:
            if preset is None:
                instance.label_preset = None
            else:
                instance.apply_preset(preset)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

    # ------------------------------------------------------------------
    # Patient notification template validation (Phase 1 of the
    # customisable-templates rollout)
    # ------------------------------------------------------------------
    #
    # Both fields go through the same allow-list check, so the rule
    # lives in a single helper. The ``find_disallowed_variables``
    # call returns a sorted distinct list of placeholder names that
    # are NOT in ``PATIENT_NOTIFICATION_ALLOWED_VARS`` — empty list
    # means "safe to save". We surface the bad names verbatim in the
    # error so the admin UI can render exactly which placeholders the
    # operator needs to remove.
    #
    # Empty templates are explicitly accepted: spec §5 requires a
    # fallback-to-default behaviour when the lab has not configured
    # templates, and the empty string is the canonical "use default"
    # signal. The renderer (Phase 2) treats it accordingly.

    def _validate_patient_template(
        self, value: str, field: str,
    ) -> str:
        if not value:
            return value
        bad = find_disallowed_variables(value)
        if bad:
            allowed_listed = ', '.join(
                f'{{{{ {name} }}}}'
                for name in sorted(PATIENT_NOTIFICATION_ALLOWED_VARS)
            )
            bad_listed = ', '.join(f'{{{{ {n} }}}}' for n in bad)
            raise serializers.ValidationError(
                f'{field}: disallowed placeholder(s) {bad_listed}. '
                f'Allowed variables are {allowed_listed}.',
            )
        return value

    def validate_patient_result_email_subject_template(self, value: str) -> str:
        return self._validate_patient_template(value, 'subject template')

    def validate_patient_result_email_body_template(self, value: str) -> str:
        return self._validate_patient_template(value, 'body template')


class LogoUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        content_type = getattr(value, 'content_type', '')
        if content_type not in _ALLOWED_LOGO_MIMES:
            raise serializers.ValidationError(
                f'Unsupported image type: {content_type or "(none)"}. '
                f'Allowed: {", ".join(sorted(_ALLOWED_LOGO_MIMES))}.'
            )
        max_size = getattr(dj_settings, 'LAB_LOGO_MAX_SIZE', _MAX_LOGO_SIZE)
        if value.size > max_size:
            max_mb = max_size / (1024 * 1024)
            raise serializers.ValidationError(
                f'File too large. Maximum size is {max_mb:.0f} MB.'
            )
        return value
