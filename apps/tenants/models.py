import uuid
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django_tenants.models import TenantMixin, DomainMixin


class Plan(models.TextChoices):
    FREE = 'FREE', 'Free'
    STARTER = 'STARTER', 'Starter'
    PRO = 'PRO', 'Pro'
    ENTERPRISE = 'ENTERPRISE', 'Enterprise'


class Tenant(TenantMixin):
    """
    Represents a medical laboratory on the Cytova platform.

    Lives in the public schema. Each Tenant record corresponds to one
    isolated PostgreSQL schema (e.g. schema_laba). django-tenants creates
    and migrates that schema automatically via auto_create_schema = True.

    The schema_name field is provided by TenantMixin and must be set
    explicitly at provisioning time (convention: 'schema_' + subdomain).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=100, unique=True)
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.STARTER)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)

    # Automatically create the PostgreSQL schema when this record is saved.
    auto_create_schema = True

    class Meta:
        verbose_name = 'Tenant'
        verbose_name_plural = 'Tenants'

    def __str__(self):
        return f'{self.name} ({self.subdomain})'

    @property
    def is_suspended(self):
        return self.suspended_at is not None and not self.is_active


class Domain(DomainMixin):
    """
    Maps a fully-qualified domain (or subdomain) to a Tenant.

    Example:
        Domain(domain='laba.cytova.io', tenant=<Tenant laba>, is_primary=True)

    In development, use entries in /etc/hosts and domains like 'laba.localhost'.
    """

    class Meta:
        verbose_name = 'Domain'
        verbose_name_plural = 'Domains'

    def __str__(self):
        return self.domain


class PlatformAdminManager(BaseUserManager):
    def create(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email address is required.')
        admin = self.model(email=self.normalize_email(email), **extra_fields)
        admin.set_password(password)
        admin.save(using=self._db)
        return admin


class PlatformAdmin(AbstractBaseUser):
    """
    Platform-level administrator. Lives in the public schema (apps.tenants is SHARED).

    Completely separate from per-tenant StaffUser. Used to manage tenant provisioning
    via the admin.cytova.io API. Does NOT use AUTH_USER_MODEL — has its own
    PlatformAdminJWTAuthentication class.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = PlatformAdminManager()

    class Meta:
        verbose_name = 'Platform Admin'
        verbose_name_plural = 'Platform Admins'

    def __str__(self):
        return self.email
