"""Add configurable logo rendering fields to LabSettings."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('lab_settings', '0003_label_print_config'),
    ]

    operations = [
        migrations.AddField(
            model_name='labsettings',
            name='logo_position',
            field=models.CharField(
                choices=[('LEFT', 'Left'), ('CENTER', 'Center'), ('RIGHT', 'Right')],
                default='RIGHT', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='labsettings',
            name='logo_max_width_mm',
            field=models.PositiveSmallIntegerField(
                default=40,
                help_text='Maximum width of the logo bounding box in mm.',
            ),
        ),
        migrations.AddField(
            model_name='labsettings',
            name='logo_max_height_mm',
            field=models.PositiveSmallIntegerField(
                default=20,
                help_text='Maximum height of the logo bounding box in mm.',
            ),
        ),
    ]
