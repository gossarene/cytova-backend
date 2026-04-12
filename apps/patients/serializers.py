"""
Cytova — Patients Serializers
"""
from rest_framework import serializers
from .models import Patient, PatientPortalAccount, Gender, DocumentType


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
            'id', 'document_type', 'document_number',
            'first_name', 'last_name', 'full_name',
            'date_of_birth', 'gender', 'nationality',
            'is_active', 'has_portal_account', 'created_at',
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
            'id', 'document_type', 'document_number',
            'first_name', 'last_name', 'full_name',
            'date_of_birth', 'gender', 'nationality',
            'phone', 'email', 'city_of_residence', 'address',
            'insurance_number',
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
    document_type = serializers.ChoiceField(choices=DocumentType.choices)
    document_number = serializers.CharField(max_length=100)
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    date_of_birth = serializers.DateField()
    gender = serializers.ChoiceField(choices=Gender.choices)
    nationality = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default='')
    email = serializers.EmailField(required=False, allow_blank=True, default='')
    city_of_residence = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    address = serializers.CharField(required=False, allow_blank=True, default='')
    insurance_number = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')

    def validate(self, attrs):
        # BR-P1: document_type + document_number unique within tenant
        doc_type = attrs.get('document_type')
        doc_number = attrs.get('document_number')
        if Patient.objects.filter(document_type=doc_type, document_number=doc_number).exists():
            raise serializers.ValidationError({
                'document_number': 'A patient with this document type and number already exists in this laboratory.'
            })
        return attrs


class PatientUpdateSerializer(serializers.Serializer):
    """
    Partial update for normal patient fields.
    Identity fields (document_type, document_number) are NOT accepted here —
    they require patients.update_identity and use PatientIdentityUpdateSerializer.
    """
    first_name = serializers.CharField(max_length=100, required=False)
    last_name = serializers.CharField(max_length=100, required=False)
    date_of_birth = serializers.DateField(required=False)
    gender = serializers.ChoiceField(choices=Gender.choices, required=False)
    nationality = serializers.CharField(max_length=100, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    city_of_residence = serializers.CharField(max_length=150, required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    insurance_number = serializers.CharField(max_length=100, required=False, allow_blank=True)


class PatientIdentityUpdateSerializer(serializers.Serializer):
    """
    Update identity fields only. Requires patients.update_identity permission.
    Validates uniqueness of the new document_type + document_number pair.
    """
    document_type = serializers.ChoiceField(choices=DocumentType.choices, required=False)
    document_number = serializers.CharField(max_length=100, required=False)

    def validate(self, attrs):
        if not attrs:
            return attrs
        # If either field is being changed, check uniqueness of the resulting pair
        patient = self.context.get('patient')
        doc_type = attrs.get('document_type', patient.document_type if patient else None)
        doc_number = attrs.get('document_number', patient.document_number if patient else None)
        qs = Patient.objects.filter(document_type=doc_type, document_number=doc_number)
        if patient:
            qs = qs.exclude(pk=patient.pk)
        if qs.exists():
            raise serializers.ValidationError({
                'document_number': 'A patient with this document type and number already exists in this laboratory.'
            })
        return attrs


class PortalAccountCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        if PatientPortalAccount.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                'A portal account with this email already exists.'
            )
        return value
