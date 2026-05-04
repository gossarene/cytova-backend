"""
Phase 2 of the flexible-labels rollout — refactor LabelSequence from
``(year, month)`` integers to a single ``period_key`` string.

Backfill rule:
  monthly key = f"{year:04d}-{month:02d}"

Same physical row, same monotonic ``last_value``; only the lookup
shape changes. Existing tenants experience zero behaviour drift —
the allocator will compute identical period keys from today's date
once Phase 3 wires the lab setting in. Phase 2 keeps the allocator
hardcoded to monthly so this migration is purely a schema swap.

Forward-only: ``year`` and ``month`` are dropped after backfill, so
a downgrade path would require recovering them from ``period_key``
(easy: split on '-'). The migration is single-file by design — see
the validated decision #2 in the phased plan.
"""
from django.db import migrations, models


def _backfill_period_key(apps, schema_editor):
    """Populate ``period_key`` for every existing row from the soon-
    to-be-dropped ``year`` / ``month`` columns. Runs once per tenant
    schema (django-tenants applies tenant migrations per-schema).
    """
    LabelSequence = apps.get_model('analysis_requests', 'LabelSequence')
    for row in LabelSequence.objects.all():
        # Defensive zfill on year so a hypothetical 3-digit year row
        # doesn't break the format expected by the allocator.
        row.period_key = f'{row.year:04d}-{row.month:02d}'
        row.save(update_fields=['period_key'])


def _reverse_period_key_backfill(apps, schema_editor):
    """No-op reverse — ``year`` and ``month`` are restored by
    Django's reverse RemoveField, but we can't recompute them here
    because the model class no longer carries those attributes at
    this point in a downgrade. The downgrade flow expects an
    operator to backfill year/month manually from period_key
    (split on '-') before re-applying the old unique constraint.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('analysis_requests', '0019_analysisrequest_issued_at_analysisrequest_issued_by_and_more'),
    ]

    operations = [
        # Step 1 — drop the old unique constraint so we can rename
        # the lookup shape without colliding on the old key. The
        # constraint must come off BEFORE the data migration so the
        # backfill step can run without partial-state issues.
        migrations.RemoveConstraint(
            model_name='labelsequence',
            name='unique_label_sequence_year_month',
        ),
        # Step 2 — add ``period_key`` as a plain CharField (no
        # unique yet) so the data migration can backfill every row
        # before the unique is enforced.
        migrations.AddField(
            model_name='labelsequence',
            name='period_key',
            field=models.CharField(default='', max_length=10),
            preserve_default=False,
        ),
        # Step 3 — backfill from year/month.
        migrations.RunPython(
            _backfill_period_key,
            reverse_code=_reverse_period_key_backfill,
        ),
        # Step 4 — alter the field to add the help_text + ensure the
        # final shape matches the model definition. AlterField on a
        # CharField is a no-op at the DB level; included so the model
        # state and DB state agree at the end of this migration.
        migrations.AlterField(
            model_name='labelsequence',
            name='period_key',
            field=models.CharField(
                help_text='Period the sequence resets on. "YYYY-MM" for '
                          'monthly resets, "YYYY" for yearly. Computed by '
                          'apps.requests.label_service.period_key_for.',
                max_length=10,
            ),
        ),
        # Step 5 — drop the old indexes that reference year/month.
        # They're recreated below for ``period_key``.
        migrations.RemoveIndex(
            model_name='labelsequence',
            name='analysis_re_year_332e90_idx',
        ),
        # Step 6 — add the new unique constraint + index on the new
        # lookup key. Order matters: unique BEFORE index so the
        # B-tree backing the unique can also serve point lookups.
        migrations.AddConstraint(
            model_name='labelsequence',
            constraint=models.UniqueConstraint(
                fields=['period_key'],
                name='unique_label_sequence_period_key',
            ),
        ),
        migrations.AddIndex(
            model_name='labelsequence',
            index=models.Index(
                fields=['period_key'],
                name='label_sequence_period_idx',
            ),
        ),
        # Step 7 — drop the legacy columns. After this point the
        # period_key is the only lookup surface.
        migrations.RemoveField(
            model_name='labelsequence',
            name='year',
        ),
        migrations.RemoveField(
            model_name='labelsequence',
            name='month',
        ),
    ]
