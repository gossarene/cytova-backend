import uuid
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone


class Role(models.TextChoices):
    LAB_ADMIN = 'LAB_ADMIN', 'Lab Admin'
    BIOLOGIST = 'BIOLOGIST', 'Biologist'
    TECHNICIAN = 'TECHNICIAN', 'Technician'
    RECEPTIONIST = 'RECEPTIONIST', 'Receptionist'
    VIEWER = 'VIEWER', 'Viewer'


class StaffUserManager(BaseUserManager):
    """Custom manager for StaffUser using email as the unique identifier."""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('An email address is required.')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role', Role.LAB_ADMIN)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self.create_user(email, password, **extra_fields)


class StaffUser(AbstractBaseUser, PermissionsMixin):
    """
    Per-tenant laboratory staff user. Lives in each tenant's private schema.

    Used as AUTH_USER_MODEL. Email is the login identifier; role drives RBAC.
    Extends PermissionsMixin for Django admin compatibility.

    Note: PermissionsMixin introduces ManyToMany relations to auth.Group and
    auth.Permission, which live in the public schema. These cross-schema
    relations work correctly because the DB search_path includes 'public'.
    They are not used for Cytova's RBAC — our Role field handles access control.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    role = models.CharField(max_length=20, choices=Role.choices)

    # Django internals
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)   # Django admin access

    # Self-referential: who created this user (null for the first Lab Admin)
    created_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_users',
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name', 'role']

    objects = StaffUserManager()

    class Meta:
        verbose_name = 'Staff User'
        verbose_name_plural = 'Staff Users'
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f'{self.get_full_name()} <{self.email}>'

    def get_full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    def get_short_name(self):
        return self.first_name

    @property
    def is_lab_admin(self):
        return self.role == Role.LAB_ADMIN


class PasswordResetToken(models.Model):
    """
    Single-use password reset token for staff users. Per-tenant.

    The plaintext token is sent to the user via email and never persisted.
    Only the SHA-256 hash is stored (via hash_token() utility).
    Tokens expire after 1 hour and are invalidated on use.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        StaffUser,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens',
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Password Reset Token'
        verbose_name_plural = 'Password Reset Tokens'

    def __str__(self):
        return f'PasswordResetToken for {self.user.email} (used={self.is_used})'
