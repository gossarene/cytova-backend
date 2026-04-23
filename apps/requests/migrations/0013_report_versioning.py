"""
Convert AnalysisRequestReport from one-per-request to many-per-request
with explicit versioning.

- Add ``version_number`` + ``is_current`` columns
- Backfill existing rows as v1, is_current=True
- Convert OneToOneField → ForeignKey (related_name: ``report`` → ``reports``)
- Enforce invariants via DB constraints
"""
from django.db import migrations, models
import django.db.models.deletion


def backfill_version_numbers(apps, schema_editor):
    AnalysisRequestReport = apps.get_model('analysis_requests', 'AnalysisRequestReport')
    for row in AnalysisRequestReport.objects.all():
        row.version_number = 1
        row.is_current = True
        row.save(update_fields=['version_number', 'is_current'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('analysis_requests', '0012_labelsequence'),
    ]

    operations = [
        # 1. Add the new columns as nullable so backfill can populate them.
        migrations.AddField(
            model_name='analysisrequestreport',
            name='version_number',
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='analysisrequestreport',
            name='is_current',
            field=models.BooleanField(
                default=True, db_index=True,
                help_text='True for the version that download and UI use. '
                          'Exactly one current version per request.',
            ),
        ),

        # 2. Backfill any pre-existing reports with v1 / current.
        migrations.RunPython(backfill_version_numbers, noop),

        # 3. Make version_number non-nullable now that rows carry a value.
        migrations.AlterField(
            model_name='analysisrequestreport',
            name='version_number',
            field=models.PositiveIntegerField(
                help_text='1-indexed version number within this request. '
                          'Increments on every regenerate action.',
            ),
        ),

        # 4. Relax the OneToOne to a ForeignKey so a request can hold
        # multiple versions. The ``related_name`` moves from ``report`` to
        # ``reports`` — call sites are updated in the same commit.
        migrations.AlterField(
            model_name='analysisrequestreport',
            name='analysis_request',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='reports',
                to='analysis_requests.analysisrequest',
            ),
        ),

        # 5. Enforce the invariants at DB level.
        migrations.AddConstraint(
            model_name='analysisrequestreport',
            constraint=models.UniqueConstraint(
                fields=('analysis_request', 'version_number'),
                name='unique_report_version_per_request',
            ),
        ),
        migrations.AddConstraint(
            model_name='analysisrequestreport',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_current', True)),
                fields=('analysis_request',),
                name='unique_current_report_per_request',
            ),
        ),
        migrations.AddIndex(
            model_name='analysisrequestreport',
            index=models.Index(
                fields=['analysis_request', '-version_number'],
                name='analysis_re_analysi_0f7a36_idx',
            ),
        ),
    ]
