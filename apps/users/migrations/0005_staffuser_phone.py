"""Add optional phone number to StaffUser (collected at onboarding for the lab admin)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0004_staffuser_signature_file_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='staffuser',
            name='phone',
            field=models.CharField(
                blank=True, default='', max_length=30,
                help_text='Contact phone number, free format. Collected at onboarding '
                          'for the lab admin; optional for other staff.',
            ),
        ),
    ]
