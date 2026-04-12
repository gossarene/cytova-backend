"""
Refactor patient identification: national_id → document_type + document_number.
Add nationality and city_of_residence fields.

Migration strategy (3 steps in one file):
1. Add new fields (document_type, document_number, nationality, city_of_residence)
   with document_number initially nullable so existing rows are valid.
2. Data migration: copy national_id → document_number, set document_type
   to NATIONAL_ID_CARD for all existing rows.
3. Remove national_id, make document_number non-nullable, add unique
   constraint on (document_type, document_number).
"""
from django.db import migrations, models


def migrate_national_id_to_document(apps, schema_editor):
    Patient = apps.get_model('patients', 'Patient')
    count = Patient.objects.filter(document_number='').update(
        document_number=models.F('national_id'),
        document_type='NATIONAL_ID_CARD',
    )
    if count:
        print(f'\n  Migrated {count} patient(s): national_id -> document_number')


def reverse_migrate(apps, schema_editor):
    Patient = apps.get_model('patients', 'Patient')
    count = Patient.objects.filter(national_id='').update(
        national_id=models.F('document_number'),
    )
    if count:
        print(f'\n  Reversed {count} patient(s): document_number -> national_id')


class Migration(migrations.Migration):
    dependencies = [
        ('patients', '0004_alter_patient_gender_choices'),
    ]

    operations = [
        # Step 1: Add new fields (document_number initially blank to allow data migration)
        migrations.AddField(
            model_name='patient',
            name='document_type',
            field=models.CharField(
                choices=[
                    ('NATIONAL_ID_CARD', 'National ID Card'),
                    ('PASSPORT', 'Passport'),
                    ('CIP', 'CIP'),
                    ('RESIDENCE_PERMIT', 'Residence Permit'),
                    ('OTHER', 'Other'),
                ],
                default='NATIONAL_ID_CARD',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='patient',
            name='document_number',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='patient',
            name='nationality',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='patient',
            name='city_of_residence',
            field=models.CharField(blank=True, default='', max_length=150),
        ),

        # Step 2: Copy national_id → document_number
        migrations.RunPython(migrate_national_id_to_document, reverse_migrate),

        # Step 3: Remove national_id, finalize document_number
        migrations.RemoveField(
            model_name='patient',
            name='national_id',
        ),
        migrations.AlterField(
            model_name='patient',
            name='document_number',
            field=models.CharField(db_index=True, max_length=100),
        ),
        migrations.AddConstraint(
            model_name='patient',
            constraint=models.UniqueConstraint(
                fields=['document_type', 'document_number'],
                name='unique_patient_document',
            ),
        ),
    ]
