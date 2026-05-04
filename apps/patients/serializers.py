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
            'identity_number_auto_generated',
            'first_name', 'last_name', 'full_name',
            'date_of_birth', 'date_of_birth_unknown',
            'gender', 'nationality',
            'is_active', 'has_portal_account', 'created_at',
        ]

    def get_full_name(self, obj):
        return obj.full_name


class PatientDetailSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    portal_account = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    # ---- Cytova patient-identity link (Phase C exposure) ------------
    # Read-only summary of the lab → global Cytova link, scoped to
    # what the lab UI legitimately needs to render the linked-state
    # badge + recovery actions:
    #
    #   - ``has_cytova_identity``                — drives badge visibility
    #     and gates the "Notify Cytova" CTA without needing the UI to
    #     check both halves of the snapshot.
    #   - ``cytova_patient_id``                  — already public to the
    #     patient (CV-XXXX-XXXX), printable on receipts. Empty when
    #     unlinked.
    #   - ``cytova_identity_verified_at``        — when the link was
    #     last confirmed via the global identity-verification service.
    #     Surfaces in the badge tooltip / detail row.
    #   - ``cytova_identity_verified_by_display``— display name of the
    #     receptionist / lab admin who linked. ``None`` after that
    #     staff user is removed (FK is SET_NULL by design).
    #   - ``cytova_identity_unlinked_at``        — surfaces only on
    #     unlinked rows that were previously linked, so the UI can
    #     show "Last unlinked at …" without inferring it.
    #
    # NOT exposed by design:
    #   - ``cytova_patient_account_id``  internal cross-schema snapshot
    #     UUID; useful only to the backend's re-verification path.
    #   - ANY field from the global ``PatientAccount`` row (email,
    #     name, DOB) — that data lives in the public schema and the
    #     lab tenant must never carry a serialised copy. The link is
    #     a *snapshot*, not a join.
    has_cytova_identity = serializers.BooleanField(read_only=True)
    cytova_identity_verified_by_display = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = [
            'id', 'document_type', 'document_number',
            # Flexible-identity rollout — surfaced read-only so the
            # UI can render a placeholder badge instead of treating
            # an auto-generated number as a real ID.
            'identity_number_auto_generated',
            'first_name', 'last_name', 'full_name',
            'date_of_birth', 'date_of_birth_unknown',
            'gender', 'nationality',
            'phone', 'email', 'city_of_residence', 'address',
            'insurance_number',
            'is_active', 'portal_account',
            'created_by', 'created_at', 'updated_at',
            # Cytova link — see the field-level rationale above.
            'has_cytova_identity', 'cytova_patient_id',
            'cytova_identity_verified_at', 'cytova_identity_verified_by_display',
            'cytova_identity_unlinked_at',
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

    def get_cytova_identity_verified_by_display(self, obj):
        """Display name of the staff user who linked this patient.
        ``None`` when the link is fresh (verified_by_id unset) or
        when the staff user has been removed (the FK is SET_NULL by
        design — verified_at survives, the now-orphaned reference
        clears). Mirrors the ``_user_display`` pattern used by
        ``apps.requests.serializers``."""
        user = obj.cytova_identity_verified_by
        return user.display_name if user is not None else None


class PatientCreateSerializer(serializers.Serializer):
    """Patient creation input.

    Flexible-identity rollout (rules from the spec):

      - **Case A** ``document_type=UNKNOWN`` → ``document_number``
        is optional. The service auto-generates an ``AUTO-PT-…``
        identifier and stamps ``identity_number_auto_generated``
        when the operator leaves it blank.
      - **Case B** ``document_type != UNKNOWN`` → ``document_number``
        is required. Operator vouches for a real document number.
      - **Case C** ``date_of_birth_unknown=True`` → ``date_of_birth``
        is optional / nullable. The serializer accepts a missing
        DOB only when this explicit flag is set, so a forgotten
        date-picker can never silently land null.
      - **Case D** ``date_of_birth_unknown=False`` →
        ``date_of_birth`` is required.

    Both flags + the auto-generated marker stay invisible on the
    *input* side (operator never sets ``identity_number_auto_generated``;
    the service decides). On the *output* side the detail
    serializer surfaces them so the UI can render appropriate
    placeholders.
    """
    document_type = serializers.ChoiceField(choices=DocumentType.choices)
    # Case A relaxation: allow blank when type is UNKNOWN. Cross-
    # field validation enforces that Case B still requires a value.
    document_number = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default='',
    )
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    # Case C/D relaxation: nullable on the wire. The validator
    # below enforces that Case D still requires the field.
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    date_of_birth_unknown = serializers.BooleanField(required=False, default=False)
    gender = serializers.ChoiceField(choices=Gender.choices)
    nationality = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default='')
    email = serializers.EmailField(required=False, allow_blank=True, default='')
    city_of_residence = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    address = serializers.CharField(required=False, allow_blank=True, default='')
    insurance_number = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')

    def validate(self, attrs):
        doc_type = attrs.get('document_type')
        doc_number = (attrs.get('document_number') or '').strip()
        dob = attrs.get('date_of_birth')
        dob_unknown = attrs.get('date_of_birth_unknown', False)

        # Case B: real document type → number required.
        if doc_type != DocumentType.UNKNOWN and not doc_number:
            raise serializers.ValidationError({
                'document_number': (
                    'Identity number is required for this document '
                    'type. Pick "Unknown / not provided" if no '
                    'document is available.'
                ),
            })

        # Case D: DOB required unless explicitly flagged unknown.
        if not dob_unknown and dob is None:
            raise serializers.ValidationError({
                'date_of_birth': (
                    'Date of birth is required. Set '
                    '"date_of_birth_unknown" to true if the DOB is '
                    'genuinely not on file.'
                ),
            })

        # Case A consistency: an unknown DOB explicitly stored as
        # ``date_of_birth_unknown=True`` MUST clear the date field
        # too — keeping a date alongside the unknown flag would let
        # downstream code make decisions on stale data.
        if dob_unknown and dob is not None:
            attrs['date_of_birth'] = None

        # BR-P1: document_type + document_number unique within
        # tenant. Skipped for UNKNOWN-with-blank-number because the
        # service will generate a fresh number that's checked at
        # insert time via the DB unique constraint.
        if doc_number:
            if Patient.objects.filter(
                document_type=doc_type, document_number=doc_number,
            ).exists():
                raise serializers.ValidationError({
                    'document_number': (
                        'A patient with this document type and number '
                        'already exists in this laboratory.'
                    ),
                })

        return attrs


class PatientUpdateSerializer(serializers.Serializer):
    """
    Partial update for normal patient fields.
    Identity fields (document_type, document_number) are NOT accepted here —
    they require patients.update_identity and use PatientIdentityUpdateSerializer.

    DOB fields ARE accepted here because flipping
    ``date_of_birth_unknown`` is a normal data-entry correction
    (operator located the DOB after initial intake) and doesn't
    require the elevated identity permission.
    """
    first_name = serializers.CharField(max_length=100, required=False)
    last_name = serializers.CharField(max_length=100, required=False)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    date_of_birth_unknown = serializers.BooleanField(required=False)
    gender = serializers.ChoiceField(choices=Gender.choices, required=False)
    nationality = serializers.CharField(max_length=100, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    city_of_residence = serializers.CharField(max_length=150, required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    insurance_number = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate(self, attrs):
        # Case C/D enforcement on partial updates. We only check
        # when EITHER ``date_of_birth`` or ``date_of_birth_unknown``
        # is in the payload — a partial update that doesn't touch
        # DOB at all leaves the existing values alone.
        patient = self.context.get('patient')
        if 'date_of_birth' in attrs or 'date_of_birth_unknown' in attrs:
            dob = attrs.get(
                'date_of_birth',
                getattr(patient, 'date_of_birth', None) if patient else None,
            )
            dob_unknown = attrs.get(
                'date_of_birth_unknown',
                getattr(patient, 'date_of_birth_unknown', False) if patient else False,
            )
            if not dob_unknown and dob is None:
                raise serializers.ValidationError({
                    'date_of_birth': (
                        'Date of birth is required. Set '
                        '"date_of_birth_unknown" to true if the DOB '
                        'is genuinely not on file.'
                    ),
                })
            # Same Case A consistency rule as create — clear the
            # date field when the operator flips the flag on.
            if dob_unknown and dob is not None:
                attrs['date_of_birth'] = None
        return attrs


class PatientIdentityUpdateSerializer(serializers.Serializer):
    """
    Update identity fields only. Requires patients.update_identity permission.
    Validates uniqueness of the new document_type + document_number pair.

    Type-transition rules (rollout spec §1):

      - ``UNKNOWN → real type`` requires ``document_number`` to be
        present in the payload (or already on the patient row).
        The service flips ``identity_number_auto_generated=False``.
      - ``real → UNKNOWN`` accepts an empty number; the service
        auto-generates a placeholder and sets the flag to True.
      - Same-type updates behave exactly as before.
    """
    document_type = serializers.ChoiceField(choices=DocumentType.choices, required=False)
    document_number = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
    )

    def validate(self, attrs):
        if not attrs:
            return attrs
        patient = self.context.get('patient')
        # Resolve the EFFECTIVE type + number after this update —
        # absent fields fall back to the patient's current values.
        doc_type = attrs.get(
            'document_type',
            patient.document_type if patient else None,
        )
        # ``document_number`` may be present-but-blank (operator
        # clearing the field). Distinguish that from "field absent
        # from payload" so we know whether to fall back to the
        # patient's current value.
        #
        # Special case: UNKNOWN → real type. The patient's current
        # ``document_number`` is an auto-generated ``AUTO-PT-…``
        # placeholder, not a real identifier. Inheriting it would
        # smuggle the placeholder into a real-type row. Force the
        # operator to supply a real number explicitly.
        if 'document_number' in attrs:
            doc_number = (attrs.get('document_number') or '').strip()
        elif (
            patient is not None
            and patient.identity_number_auto_generated
            and doc_type != DocumentType.UNKNOWN
        ):
            doc_number = ''
        else:
            doc_number = patient.document_number if patient else ''

        # UNKNOWN → real type without a number → reject.
        # The service can't auto-generate against a real type — that
        # would be misleading.
        if doc_type != DocumentType.UNKNOWN and not doc_number:
            raise serializers.ValidationError({
                'document_number': (
                    'Identity number is required for this document '
                    'type. Pick "Unknown / not provided" if no '
                    'document is available.'
                ),
            })

        # Within-tenant uniqueness check. Skip when the resulting
        # number would be empty (UNKNOWN + blank — service will
        # generate a fresh value).
        if doc_number:
            qs = Patient.objects.filter(
                document_type=doc_type, document_number=doc_number,
            )
            if patient:
                qs = qs.exclude(pk=patient.pk)
            if qs.exists():
                raise serializers.ValidationError({
                    'document_number': (
                        'A patient with this document type and number '
                        'already exists in this laboratory.'
                    ),
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


class CytovaIdentityLinkSerializer(serializers.Serializer):
    """Input shape for ``POST /patients/{id}/link-cytova-identity/``.

    Mirrors ``apps.requests.serializers.NotifyCytovaSerializer`` field
    by field — the same identity-verification call site consumes both,
    so the input contract stays consistent across the two surfaces. No
    field-level validation lives here: any normalisation /
    canonicalisation happens inside the lookup layer (so the lab
    tenant never has its own copy of the rules).
    """
    cytova_patient_id = serializers.CharField(max_length=32)
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    date_of_birth = serializers.DateField()
