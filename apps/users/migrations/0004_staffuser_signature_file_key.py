"""Add signature image storage key to StaffUser."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0003_staffuser_title'),
    ]

    operations = [
        migrations.AddField(
            model_name='staffuser',
            name='signature_file_key',
            field=models.CharField(
                blank=True, default='', max_length=500,
                help_text="Internal storage key for the user's signature image. "
                          "Rendered on reports validated by this user.",
            ),
        ),
    ]
