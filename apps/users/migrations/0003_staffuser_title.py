"""Add optional professional title to StaffUser."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_rbac_permissions'),
    ]

    operations = [
        migrations.AddField(
            model_name='staffuser',
            name='title',
            field=models.CharField(
                blank=True, default='', max_length=20,
                help_text='Professional title (e.g. "Dr", "Pr"). Displayed on '
                          'signed documents such as final reports.',
            ),
        ),
    ]
