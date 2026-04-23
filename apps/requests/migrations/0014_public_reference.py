"""
Introduce the patient-facing ``public_reference`` on AnalysisRequest and
its per-day sequence allocator ``RequestReferenceSequence``.

Backfill strategy (dev phase): existing AnalysisRequest rows are
assigned a reference deterministically by ``created_at`` date + a
per-day sequential number. The counter rows are populated in the same
pass so future allocations pick up where backfill left off.
"""
from collections import defaultdict

from django.db import migrations, models


def backfill_public_references(apps, schema_editor):
    AnalysisRequest = apps.get_model('analysis_requests', 'AnalysisRequest')
    RequestReferenceSequence = apps.get_model(
        'analysis_requests', 'RequestReferenceSequence',
    )

    per_day_counts: dict = defaultdict(int)
    for ar in AnalysisRequest.objects.order_by('created_at', 'id'):
        d = ar.created_at.date()
        per_day_counts[d] += 1
        seq = per_day_counts[d]
        ar.public_reference = f'{d.strftime("%Y%m%d")}-{seq:06d}'
        ar.save(update_fields=['public_reference'])

    for d, last_value in per_day_counts.items():
        RequestReferenceSequence.objects.update_or_create(
            date=d, defaults={'last_value': last_value},
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('analysis_requests', '0013_report_versioning'),
    ]

    operations = [
        migrations.CreateModel(
            name='RequestReferenceSequence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(unique=True)),
                ('last_value', models.PositiveIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Request Reference Sequence',
                'verbose_name_plural': 'Request Reference Sequences',
            },
        ),
        migrations.AlterField(
            model_name='analysisrequest',
            name='request_number',
            field=models.CharField(
                blank=True, db_index=True, default='', max_length=30, unique=True,
                help_text='Internal/system identifier (e.g. REQ-2026-A2AF70DE). '
                          'Used for audit logs and backend debugging.',
            ),
        ),
        # Added without db_index=True on purpose — the final AlterField below
        # flips the column to unique=True which already creates the needed
        # B-tree + LIKE indexes. Adding db_index=True here would cause a
        # duplicate index error on the subsequent unique alter.
        migrations.AddField(
            model_name='analysisrequest',
            name='public_reference',
            field=models.CharField(
                blank=True, default='', max_length=20, null=True,
                help_text='Clean patient-facing reference (YYYYMMDD-NNNNNN). '
                          'Used on final reports and other external documents.',
            ),
        ),
        migrations.RunPython(backfill_public_references, noop),
        migrations.AlterField(
            model_name='analysisrequest',
            name='public_reference',
            field=models.CharField(
                blank=True, default='', max_length=20, unique=True,
                help_text='Clean patient-facing reference (YYYYMMDD-NNNNNN). '
                          'Used on final reports and other external documents.',
            ),
        ),
    ]
