"""Add label-printing effective config to LabSettings."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('lab_settings', '0002_add_logo_url'),
        ('labels', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='labsettings',
            name='label_print_mode',
            field=models.CharField(
                choices=[('A4_SHEET', 'A4 Multi-Label Sheet'),
                         ('THERMAL_ROLL', 'Thermal Roll')],
                default='A4_SHEET', max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='labsettings',
            name='label_preset',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='labels.labelprintpreset',
                help_text='Last preset applied. Kept as an audit reference only — '
                          'effective values live on this row and are authoritative.',
            ),
        ),
        migrations.AddField(model_name='labsettings', name='label_page_width_mm',
                            field=models.PositiveSmallIntegerField(default=210)),
        migrations.AddField(model_name='labsettings', name='label_page_height_mm',
                            field=models.PositiveSmallIntegerField(default=297)),
        migrations.AddField(model_name='labsettings', name='label_label_width_mm',
                            field=models.PositiveSmallIntegerField(default=90)),
        migrations.AddField(model_name='labsettings', name='label_label_height_mm',
                            field=models.PositiveSmallIntegerField(default=50)),
        migrations.AddField(model_name='labsettings', name='label_margin_top_mm',
                            field=models.PositiveSmallIntegerField(default=15)),
        migrations.AddField(model_name='labsettings', name='label_margin_left_mm',
                            field=models.PositiveSmallIntegerField(default=10)),
        migrations.AddField(model_name='labsettings', name='label_horizontal_gap_mm',
                            field=models.PositiveSmallIntegerField(default=5)),
        migrations.AddField(model_name='labsettings', name='label_vertical_gap_mm',
                            field=models.PositiveSmallIntegerField(default=5)),
        migrations.AddField(model_name='labsettings', name='label_thermal_gap_mm',
                            field=models.PositiveSmallIntegerField(default=2)),
        migrations.AddField(model_name='labsettings', name='label_show_barcode',
                            field=models.BooleanField(default=True)),
        migrations.AddField(model_name='labsettings', name='label_show_numeric_code',
                            field=models.BooleanField(default=True)),
    ]
