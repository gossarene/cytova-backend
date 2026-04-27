"""
Cytova — Laboratory Onboarding Serializers (Public API)

Step-by-step onboarding: identity → email verification → lab info → password.
Each step has its own serializer; tenant creation only happens after all
four steps validate.
"""
import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.tenants.models import Tenant


# Allowed slug characters: lowercase letters, digits, hyphens (no leading/trailing hyphen)
SLUG_RE = re.compile(r'^[a-z][a-z0-9-]*[a-z0-9]$')

# Permissive phone format: optional leading +, then digits/spaces/-/()/. — matches
# the most common international and local conventions without forcing E.164.
PHONE_RE = re.compile(r'^\+?[\d\s().\-]{5,30}$')

# 6-digit numeric verification code.
CODE_RE = re.compile(r'^\d{6}$')

# Reserved subdomains that cannot be used by tenants
RESERVED_SLUGS = frozenset({
    'admin', 'api', 'app', 'auth', 'www', 'mail', 'ftp', 'smtp',
    'pop', 'imap', 'ns1', 'ns2', 'cdn', 'static', 'assets', 'media',
    'staging', 'dev', 'test', 'demo', 'status', 'help', 'support',
    'docs', 'blog', 'shop', 'store', 'billing', 'pay', 'portal',
    'public', 'platform', 'internal', 'system', 'root', 'cytova',
})


# ---------------------------------------------------------------------------
# Step 1 — Identity
# ---------------------------------------------------------------------------

class OnboardingStartSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=30)

    def validate_phone(self, value):
        phone = (value or '').strip()
        if not PHONE_RE.match(phone):
            raise serializers.ValidationError(
                'Enter a valid phone number (digits, spaces, +, -, (), . allowed; 5–30 chars).'
            )
        return phone

    def validate_email(self, value):
        return value.strip().lower()


# ---------------------------------------------------------------------------
# Step 2 — Email verification
# ---------------------------------------------------------------------------

class OnboardingVerifyEmailSerializer(serializers.Serializer):
    registration_id = serializers.UUIDField()
    code = serializers.CharField(min_length=6, max_length=6)

    def validate_code(self, value):
        code = (value or '').strip()
        if not CODE_RE.match(code):
            raise serializers.ValidationError('Code must be exactly 6 digits.')
        return code


class OnboardingResendCodeSerializer(serializers.Serializer):
    registration_id = serializers.UUIDField()


# ---------------------------------------------------------------------------
# Step 4 — Complete (lab info + password). Step 3 is purely client-side
# until the user submits the final step, so there is no separate
# step-3 serializer.
# ---------------------------------------------------------------------------

class OnboardingCompleteSerializer(serializers.Serializer):
    registration_id = serializers.UUIDField()

    laboratory_name = serializers.CharField(max_length=255)
    country = serializers.CharField(min_length=2, max_length=2)
    city = serializers.CharField(max_length=120)
    slug = serializers.CharField(max_length=63, required=False)

    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
        min_length=12,
    )

    # ----- Field-level validation --------------------------------------

    def validate_country(self, value):
        country = (value or '').strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise serializers.ValidationError(
                'Country must be a 2-letter ISO 3166-1 alpha-2 code (e.g. "FR").'
            )
        return country

    def validate_city(self, value):
        city = (value or '').strip()
        if len(city) < 2:
            raise serializers.ValidationError('City must be at least 2 characters.')
        return city

    def validate_slug(self, value):
        slug = (value or '').lower().strip()
        if len(slug) < 3:
            raise serializers.ValidationError('Slug must be at least 3 characters.')
        if len(slug) > 63:
            raise serializers.ValidationError('Slug must not exceed 63 characters (DNS label limit).')
        if not SLUG_RE.match(slug):
            raise serializers.ValidationError(
                'Slug must start with a letter, contain only lowercase letters, '
                'digits, and hyphens, and not end with a hyphen.'
            )
        if slug in RESERVED_SLUGS:
            raise serializers.ValidationError(f'"{slug}" is a reserved name and cannot be used.')
        if Tenant.objects.filter(subdomain=slug).exists():
            raise serializers.ValidationError('A laboratory with this identifier already exists.')
        return slug

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate(self, attrs):
        # Auto-generate slug from laboratory name if not provided.
        if 'slug' not in attrs or not attrs.get('slug'):
            attrs['slug'] = self.validate_slug(self._slugify(attrs['laboratory_name']))
        return attrs

    @staticmethod
    def _slugify(name: str) -> str:
        """ "Hôpital Saint-Luc" → "hopital-saint-luc" """
        import unicodedata
        slug = unicodedata.normalize('NFKD', name)
        slug = slug.encode('ascii', 'ignore').decode('ascii')
        slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')
        slug = re.sub(r'-{2,}', '-', slug)
        return slug[:63].rstrip('-') or 'lab'


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------

class OnboardingRegistrationSerializer(serializers.Serializer):
    """Response for start / verify / resend endpoints."""
    registration_id = serializers.UUIDField(source='id')
    email = serializers.EmailField()
    status = serializers.CharField()
    email_verified_at = serializers.DateTimeField(allow_null=True)
    code_expires_at = serializers.DateTimeField(allow_null=True)


class LaboratorySignupResponseSerializer(serializers.Serializer):
    """Response for the final completion step. Same shape the previous
    monolithic signup endpoint used — the frontend success screen and any
    other consumers don't need to change."""
    tenant_id = serializers.UUIDField()
    laboratory_name = serializers.CharField()
    slug = serializers.CharField()
    domain = serializers.CharField()
    admin_email = serializers.EmailField()
    trial_end_date = serializers.DateTimeField(allow_null=True, required=False)
    trial_duration_days = serializers.IntegerField(allow_null=True, required=False)
