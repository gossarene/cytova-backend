# Generated manually — RBAC permissions system

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


def rename_viewer_to_viewer_auditor(apps, schema_editor):
    """Migrate VIEWER role to VIEWER_AUDITOR across all tenant schemas."""
    StaffUser = apps.get_model('users', 'StaffUser')
    StaffUser.objects.filter(role='VIEWER').update(role='VIEWER_AUDITOR')


def rename_viewer_auditor_to_viewer(apps, schema_editor):
    """Reverse migration: VIEWER_AUDITOR back to VIEWER."""
    StaffUser = apps.get_model('users', 'StaffUser')
    StaffUser.objects.filter(role='VIEWER_AUDITOR').update(role='VIEWER')


NEW_ROLE_CHOICES = [
    ('LAB_ADMIN', 'Lab Admin'),
    ('BIOLOGIST', 'Biologist'),
    ('TECHNICIAN', 'Technician'),
    ('RECEPTIONIST', 'Receptionist'),
    ('BILLING_OFFICER', 'Billing Officer'),
    ('INVENTORY_MANAGER', 'Inventory Manager'),
    ('VIEWER_AUDITOR', 'Viewer / Auditor'),
]


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        # Step 1: Widen role field to accept new values (max_length 20 -> 30)
        # and update choices to include new roles.
        migrations.AlterField(
            model_name='staffuser',
            name='role',
            field=models.CharField(
                choices=NEW_ROLE_CHOICES,
                max_length=30,
            ),
        ),

        # Step 2: Data migration — rename VIEWER to VIEWER_AUDITOR
        migrations.RunPython(
            rename_viewer_to_viewer_auditor,
            rename_viewer_auditor_to_viewer,
        ),

        # Step 3: Create UserPermissionOverride table
        migrations.CreateModel(
            name='UserPermissionOverride',
            fields=[
                ('id', models.UUIDField(
                    default=uuid.uuid4, editable=False, primary_key=True, serialize=False,
                )),
                ('permission_code', models.CharField(db_index=True, max_length=80)),
                ('override_type', models.CharField(
                    choices=[('GRANT', 'Grant'), ('REVOKE', 'Revoke')],
                    max_length=10,
                )),
                ('reason', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('granted_by', models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='granted_overrides',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='permission_overrides',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'User Permission Override',
                'verbose_name_plural': 'User Permission Overrides',
            },
        ),

        # Step 4: Unique constraint on (user, permission_code)
        migrations.AddConstraint(
            model_name='userpermissionoverride',
            constraint=models.UniqueConstraint(
                fields=['user', 'permission_code'],
                name='unique_user_permission_override',
            ),
        ),
    ]
