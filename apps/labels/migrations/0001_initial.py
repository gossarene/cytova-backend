import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='LabelPrintPreset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100)),
                ('code', models.CharField(max_length=50, unique=True,
                                          help_text='Stable machine identifier, e.g. SYS_A4_10_LABELS.')),
                ('print_mode', models.CharField(
                    choices=[('A4_SHEET', 'A4 Multi-Label Sheet'),
                             ('THERMAL_ROLL', 'Thermal Roll')],
                    db_index=True, max_length=20)),
                ('page_width_mm', models.PositiveSmallIntegerField()),
                ('page_height_mm', models.PositiveSmallIntegerField()),
                ('label_width_mm', models.PositiveSmallIntegerField()),
                ('label_height_mm', models.PositiveSmallIntegerField()),
                ('margin_top_mm', models.PositiveSmallIntegerField(default=0)),
                ('margin_left_mm', models.PositiveSmallIntegerField(default=0)),
                ('horizontal_gap_mm', models.PositiveSmallIntegerField(default=0)),
                ('vertical_gap_mm', models.PositiveSmallIntegerField(default=0)),
                ('thermal_gap_mm', models.PositiveSmallIntegerField(default=0)),
                ('show_barcode', models.BooleanField(default=True)),
                ('show_numeric_code', models.BooleanField(default=True)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('is_system', models.BooleanField(default=False,
                                                   help_text='True for platform-seeded factory presets.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Label Print Preset',
                'verbose_name_plural': 'Label Print Presets',
                'ordering': ['print_mode', 'name'],
            },
        ),
    ]
