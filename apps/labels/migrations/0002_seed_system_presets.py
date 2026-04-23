"""Seed the two factory ``LabelPrintPreset`` rows shipped with Cytova."""
from django.db import migrations

from apps.labels.defaults import A4_DEFAULTS, THERMAL_DEFAULTS


SYSTEM_PRESETS = [
    {
        'code': 'SYS_A4_10_LABELS',
        'name': 'A4 sheet — 10 labels (2×5)',
        'print_mode': 'A4_SHEET',
        **A4_DEFAULTS,
    },
    {
        'code': 'SYS_THERMAL_40X25',
        'name': 'Thermal roll — 40 × 25 mm',
        'print_mode': 'THERMAL_ROLL',
        **THERMAL_DEFAULTS,
    },
]


def seed_presets(apps, schema_editor):
    LabelPrintPreset = apps.get_model('labels', 'LabelPrintPreset')
    for data in SYSTEM_PRESETS:
        LabelPrintPreset.objects.update_or_create(
            code=data['code'],
            defaults={**data, 'is_system': True, 'is_active': True},
        )


def unseed_presets(apps, schema_editor):
    LabelPrintPreset = apps.get_model('labels', 'LabelPrintPreset')
    LabelPrintPreset.objects.filter(
        code__in=[p['code'] for p in SYSTEM_PRESETS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('labels', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_presets, unseed_presets),
    ]
