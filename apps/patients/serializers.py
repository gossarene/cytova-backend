"""
Cytova — Patients Serializers
"""
from rest_framework import serializers
from .models import Patient, PatientPortalAccount, Gender


class PortalAccountSerializer(serializers.ModelSerializer):
    """Read-only representation of a portal account (no password exposed)."""

    class Meta:
        model = PatientPortalAccount
        fields = ['id', 'email', 'is_active', 'created_at', 'last_login']


class PatientListSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    has_portal_account = serializers.BooleanField(read_only=True)

    class Meta:
        model = Patient
        fields = [
            'id', 'national_id', 'first_name', 'last_name', 'full_name',
            'date_of_birth', 'gender', 'is_active', 'has_portal_account',
            'created_at',
        ]

    def get_full_name(self, obj):
        return obj.full_name


class PatientDetailSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    portal_account = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = [
            'id', 'national_id', 'first_name', 'last_name', 'full_name',
            'date_of_birth', 'gender',
            'phone', 'email', 'address', 'insurance_number',
            'is_active', 'portal_account',
            'created_by', 'created_at', 'updated_at',
        ]

    def get_full_name(self, obj):
        return obj.full_name

    def get_portal_account(self, obj):
        try:
            account = obj.portal_account
        except PatientPortalAccount.DoesNotExist:
            return None
        return PortalAccountSerializer(account).data

    def get_created_by(self, obj):
        if obj.created_by_id:
            return {
                'id': str(obj.created_by_id),
                'email': obj.created_by.email if obj.created_by else None,
            }
        return None


class PatientCreateSerializer(serializers.Serializer):
    national_id = serializers.CharField(max_length=100)
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    date_of_birth = serializers.DateField()
    gender = serializers.ChoiceField(choices=Gender.choices)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default='')
    email = serializers.EmailField(required=False, allow_blank=True, default='')
    address = serializers.CharField(required=False, allow_blank=True, default='')
    insurance_number = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')

    def validate_national_id(self, value):
        # BR-P1: national_id unique within tenant — schema isolation enforces the scope
        if Patient.objects.filter(national_id=value).exists():
            raise serializers.ValidationError(
                'A patient with this national ID already exists in this laboratory.'
            )
        return value


class PatientUpdateSerializer(serializers.Serializer):
    """
    Partial update. national_id is intentionally excluded — it is immutable
    once a patient is registered (changing it would break audit trail linkage).
    """
    first_name = serializers.CharField(max_length=100, required=False)
    last_name = serializers.CharField(max_length=100, required=False)
    date_of_birth = serializers.DateField(required=False)
    gender = serializers.ChoiceField(choices=Gender.choices, required=False)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    insurance_number = serializers.CharField(max_length=100, required=False, allow_blank=True)


class PortalAccountCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        if PatientPortalAccount.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                'A portal account with this email already exists.'
            )
        return value
