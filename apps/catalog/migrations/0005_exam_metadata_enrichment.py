"""
Enrich exam catalog with structured metadata:
- ExamFamily (replaces ExamCategory concept)
- ExamSubFamily
- TubeType
- ExamTechnique
- fasting_required boolean on ExamDefinition

Migration strategy:
1. Create new reference models
2. Add new FK fields + fasting_required on ExamDefinition (nullable)
3. Data migrate: create ExamFamily from each ExamCategory, link ExamDefinition.family
4. ExamDefinition.category FK is made nullable (kept for now, full removal in future)
"""
import uuid
import django.db.models.deletion
from django.db import migrations, models

from common.models import BaseModel


def migrate_category_to_family(apps, schema_editor):
    ExamCategory = apps.get_model('catalog', 'ExamCategory')
    ExamFamily = apps.get_model('catalog', 'ExamFamily')
    ExamDefinition = apps.get_model('catalog', 'ExamDefinition')

    for cat in ExamCategory.objects.all():
        family, _ = ExamFamily.objects.get_or_create(
            name=cat.name,
            defaults={
                'description': cat.description,
                'display_order': cat.display_order,
                'is_active': cat.is_active,
            },
        )
        ExamDefinition.objects.filter(category_id=cat.id).update(family_id=family.id)

    count = ExamDefinition.objects.filter(family__isnull=False).count()
    if count:
        print(f'\n  Migrated {count} exam(s): category -> family')


def reverse_family_to_category(apps, schema_editor):
    pass  # Cannot reliably reverse


class Migration(migrations.Migration):
    dependencies = [
        ('catalog', '0004_rename_catalog_pri_exam_de_active_idx_catalog_pri_exam_de_7e5d8c_idx_and_more'),
        ('users', '0002_rbac_permissions'),
    ]

    operations = [
        # Step 1: Create reference models
        migrations.CreateModel(
            name='ExamFamily',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=150, unique=True)),
                ('description', models.TextField(blank=True, default='')),
                ('display_order', models.IntegerField(db_index=True, default=0)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
            ],
            options={
                'verbose_name': 'Exam Family',
                'verbose_name_plural': 'Exam Families',
                'ordering': ['display_order', 'name'],
            },
        ),
        migrations.CreateModel(
            name='ExamSubFamily',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=150)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('family', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sub_families', to='catalog.examfamily')),
            ],
            options={
                'verbose_name': 'Exam Sub-Family',
                'verbose_name_plural': 'Exam Sub-Families',
                'ordering': ['family__display_order', 'name'],
            },
        ),
        migrations.AddConstraint(
            model_name='examsubfamily',
            constraint=models.UniqueConstraint(fields=['family', 'name'], name='unique_subfamily_per_family'),
        ),
        migrations.CreateModel(
            name='TubeType',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(db_index=True, default=True)),
            ],
            options={
                'verbose_name': 'Tube Type',
                'verbose_name_plural': 'Tube Types',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='ExamTechnique',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=150, unique=True)),
                ('description', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(db_index=True, default=True)),
            ],
            options={
                'verbose_name': 'Exam Technique',
                'verbose_name_plural': 'Exam Techniques',
                'ordering': ['name'],
            },
        ),

        # Step 2: Add new fields to ExamDefinition
        migrations.AddField(
            model_name='examdefinition',
            name='family',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='exam_definitions', to='catalog.examfamily'),
        ),
        migrations.AddField(
            model_name='examdefinition',
            name='sub_family',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='exams', to='catalog.examsubfamily'),
        ),
        migrations.AddField(
            model_name='examdefinition',
            name='tube_type',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='exams', to='catalog.tubetype'),
        ),
        migrations.AddField(
            model_name='examdefinition',
            name='technique',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='exams', to='catalog.examtechnique'),
        ),
        migrations.AddField(
            model_name='examdefinition',
            name='fasting_required',
            field=models.BooleanField(default=False, help_text='Whether the patient must fast before specimen collection.'),
        ),

        # Step 3: Make category nullable (will be removed in future migration)
        migrations.AlterField(
            model_name='examdefinition',
            name='category',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='exams', to='catalog.examcategory'),
        ),

        # Step 4: Data migrate categories -> families
        migrations.RunPython(migrate_category_to_family, reverse_family_to_category),
    ]
