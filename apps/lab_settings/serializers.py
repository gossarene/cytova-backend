from django.conf import settings as dj_settings
from rest_framework import serializers

from apps.labels.models import LabelPrintPreset
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
