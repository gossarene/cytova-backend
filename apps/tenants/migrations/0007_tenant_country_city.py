"""Add country (ISO alpha-2) and city to Tenant for onboarding."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0006_tenant_numeric_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='country',
            field=models.CharField(
                blank=True, default='', max_length=2,
                help_text='ISO 3166-1 alpha-2 country code (e.g. "FR", "US").',
            ),
        ),
        migrations.AddField(
            model_name='tenant',
            name='city',
            field=models.CharField(
                blank=True, default='', max_length=120,
                help_text='Primary city of the laboratory.',
            ),
        ),
    ]
