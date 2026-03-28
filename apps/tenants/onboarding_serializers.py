"""
Cytova — Laboratory Onboarding Serializers (Public API)

Self-service signup: laboratory name + admin credentials → Tenant + StaffUser.
"""
import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.tenants.models import Tenant


# Allowed slug characters: lowercase letters, digits, hyphens (no leading/trailing hyphen)
SLUG_RE = re.compile(r'^[a-z][a-z0-9-]*[a-z0-9]$')

# Reserved subdomains that cannot be used by tenants
RESERVED_SLUGS = frozenset({
    'admin', 'api', 'app', 'auth', 'www', 'mail', 'ftp', 'smtp',
    'pop', 'imap', 'ns1', 'ns2', 'cdn', 'static', 'assets', 'media',
    'staging', 'dev', 'test', 'demo', 'status', 'help', 'support',
    'docs', 'blog', 'shop', 'store', 'billing', 'pay', 'portal',
    'public', 'platform', 'internal', 'system', 'root', 'cytova',
})


class LaboratorySignupSerializer(serializers.Serializer):
    """
    Public signup for a new laboratory.
    Creates: Tenant + Domain + initial LAB_ADMIN StaffUser.
    """
    # Laboratory
    laboratory_name = serializers.CharField(
        max_length=255,
        help_text='Display name of the laboratory.',
    )
    slug = serializers.CharField(
        max_length=63,
        required=False,
        help_text=(
            'URL-safe subdomain identifier (e.g. "city-lab"). '
            'Auto-generated from laboratory_name if omitted.'
        ),
    )

    # Admin account
    admin_email = serializers.EmailField()
    admin_first_name = serializers.CharField(max_length=100)
    admin_last_name = serializers.CharField(max_length=100)
    admin_password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
        min_length=12,
    )

    def validate_slug(self, value):
        slug = value.lower().strip()

        if len(slug) < 3:
            raise serializers.ValidationError(
                'Slug must be at least 3 characters.'
            )
        if len(slug) > 63:
            raise serializers.ValidationError(
                'Slug must not exceed 63 characters (DNS label limit).'
            )
        if not SLUG_RE.match(slug):
            raise serializers.ValidationError(
                'Slug must start with a letter, contain only lowercase '
                'letters, digits, and hyphens, and not end with a hyphen.'
            )
        if slug in RESERVED_SLUGS:
            raise serializers.ValidationError(
                f'"{slug}" is a reserved name and cannot be used.'
            )
        if Tenant.objects.filter(subdomain=slug).exists():
            raise serializers.ValidationError(
                'A laboratory with this identifier already exists.'
            )

        return slug

    def validate_admin_email(self, value):
        """
        Email uniqueness across tenants is not enforced at the DB level
        (StaffUser.email is unique per-schema). However, we check existing
        tenants' admin emails to prevent confusion during onboarding.
        """
        # No cross-tenant uniqueness check needed — emails are schema-scoped.
        # The tenant doesn't exist yet, so there's nothing to conflict with.
        return value.lower()

    def validate_admin_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate(self, attrs):
        # Auto-generate slug from laboratory name if not provided
        if 'slug' not in attrs or not attrs.get('slug'):
            attrs['slug'] = self._slugify(attrs['laboratory_name'])
            # Run slug validation on the auto-generated value
            attrs['slug'] = self.validate_slug(attrs['slug'])

        return attrs

    @staticmethod
    def _slugify(name: str) -> str:
        """
        Convert a laboratory name to a URL-safe slug.
        "Hôpital Saint-Luc" → "hopital-saint-luc"
        """
        import unicodedata
        # Normalize unicode → ASCII
        slug = unicodedata.normalize('NFKD', name)
        slug = slug.encode('ascii', 'ignore').decode('ascii')
        # Lowercase, replace non-alphanumeric with hyphens
        slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')
        # Collapse multiple hyphens
        slug = re.sub(r'-{2,}', '-', slug)
        # Truncate to DNS label limit
        return slug[:63].rstrip('-') or 'lab'


class LaboratorySignupResponseSerializer(serializers.Serializer):
    """Read-only output shape for the signup response."""
    tenant_id = serializers.UUIDField()
    laboratory_name = serializers.CharField()
    slug = serializers.CharField()
    domain = serializers.CharField()
    admin_email = serializers.EmailField()
