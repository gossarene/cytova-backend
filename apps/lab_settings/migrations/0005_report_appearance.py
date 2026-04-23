"""Add controlled report appearance settings."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('lab_settings', '0004_logo_rendering_config'),
    ]

    operations = [
        migrations.AddField(
            model_name='labsettings',
            name='report_accent_color',
            field=models.CharField(
                blank=True, default='#0f172a', max_length=7,
                help_text='Hex color for family section titles and accent lines '
                          '(e.g. "#0f172a"). Must be a valid 7-char hex code.',
            ),
        ),
        migrations.AddField(
            model_name='labsettings',
            name='show_family_divider_line',
            field=models.BooleanField(
                default=True,
                help_text='Draw a thin horizontal line below each exam family title.',
            ),
        ),
        migrations.AddField(
            model_name='labsettings',
            name='show_previous_results',
            field=models.BooleanField(
                default=True,
                help_text='Include the previous result column in report tables.',
            ),
        ),
    ]
