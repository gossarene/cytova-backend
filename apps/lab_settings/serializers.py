from django.conf import settings as dj_settings
from rest_framework import serializers
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
            'signature_file_key', 'legal_footer',
            # display options
            'show_logo', 'show_lab_address', 'show_prescriber',
            'show_collection_datetime', 'show_patient_age', 'show_patient_sex',
            'show_exam_technique', 'show_reference_ranges',
            'show_patient_comments', 'show_final_conclusion',
            'show_signature', 'show_legal_footer', 'show_abnormal_flags',
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
    class Meta:
        model = LabSettings
        fields = [
            'lab_name', 'lab_subtitle', 'logo_url',
            'address', 'phone', 'email', 'website',
            'signature_file_key', 'legal_footer',
            'show_logo', 'show_lab_address', 'show_prescriber',
            'show_collection_datetime', 'show_patient_age', 'show_patient_sex',
            'show_exam_technique', 'show_reference_ranges',
            'show_patient_comments', 'show_final_conclusion',
            'show_signature', 'show_legal_footer', 'show_abnormal_flags',
        ]
        extra_kwargs = {f: {'required': False} for f in fields}


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
