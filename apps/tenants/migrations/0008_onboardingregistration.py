"""Add OnboardingRegistration — pre-tenant signup state in the public schema."""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0007_tenant_country_city'),
    ]

    operations = [
        migrations.CreateModel(
            name='OnboardingRegistration',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('first_name', models.CharField(max_length=100)),
                ('last_name', models.CharField(max_length=100)),
                ('email', models.EmailField(db_index=True, max_length=254)),
                ('phone', models.CharField(blank=True, default='', max_length=30)),
                ('verification_code_hash', models.CharField(blank=True, default='', max_length=128)),
                ('code_expires_at', models.DateTimeField(blank=True, null=True)),
                ('failed_attempts', models.PositiveSmallIntegerField(default=0)),
                ('locked_until', models.DateTimeField(blank=True, null=True)),
                ('last_code_sent_at', models.DateTimeField(blank=True, null=True)),
                ('email_verified_at', models.DateTimeField(blank=True, null=True)),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('PENDING_EMAIL', 'Pending email verification'),
                            ('EMAIL_VERIFIED', 'Email verified'),
                            ('COMPLETED', 'Completed'),
                            ('EXPIRED', 'Expired'),
                        ],
                        db_index=True,
                        default='PENDING_EMAIL',
                        max_length=20,
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'tenant',
                    models.OneToOneField(
                        blank=True,
                        help_text='Set at completion time — points to the tenant that was created from this registration.',
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name='onboarding_registration',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Onboarding Registration',
                'verbose_name_plural': 'Onboarding Registrations',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['email', 'status'], name='tenants_onb_email_0bf463_idx')],
            },
        ),
    ]
