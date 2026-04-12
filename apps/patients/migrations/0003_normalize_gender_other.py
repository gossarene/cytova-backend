"""
Data migration: normalize any existing gender='OTHER' rows to a safe value.

Product decision: only MALE and FEMALE are supported going forward.
Rows with OTHER are set to MALE as a safe default; operators can correct
individual records via the admin or API.

This migration runs before the Gender enum is narrowed on the model field,
so that no rows violate the new constraint.
"""
from django.db import migrations


def normalize_gender_other(apps, schema_editor):
    Patient = apps.get_model('patients', 'Patient')
    updated = Patient.objects.filter(gender='OTHER').update(gender='MALE')
    if updated:
        print(f'\n  Normalized {updated} patient(s) with gender=OTHER -> MALE')


def reverse_noop(apps, schema_editor):
    # Cannot reliably reverse: we do not know which rows were originally OTHER
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('patients', '0002_initial'),
    ]

    operations = [
        migrations.RunPython(normalize_gender_other, reverse_noop),
    ]
