"""
Schema migration: narrow gender field choices to MALE / FEMALE only.

Runs after 0003 (data normalization), so no rows contain OTHER.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('patients', '0003_normalize_gender_other'),
    ]

    operations = [
        migrations.AlterField(
            model_name='patient',
            name='gender',
            field=models.CharField(
                choices=[('MALE', 'Male'), ('FEMALE', 'Female')],
                max_length=10,
            ),
        ),
    ]
