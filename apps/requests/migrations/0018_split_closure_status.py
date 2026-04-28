"""Split workflow status from post-processing closure state.

  - Adds the new ``closure_status`` field (default OPEN).
  - Backfills any existing rows that landed in the legacy DELIVERED/ARCHIVED
    workflow status: workflow ``status`` is rewound to a sane medical state,
    closure_status is set to the corresponding closure value, so historical
    meaning is preserved without polluting billing queries.
  - Tightens ``status`` choices to remove DELIVERED/ARCHIVED — those values
    can never reappear in the workflow column going forward.

Backfill rules:

  status=DELIVERED → status=VALIDATED, closure_status=DELIVERED
       (the only path into the legacy DELIVERED state was VALIDATED→DELIVERED
       via auto-deliver on email or manual mark-delivered from VALIDATED;
       legacy COMPLETED→DELIVERED is rare and treating it as VALIDATED is
       still semantically faithful to "validated and delivered to patient")

  status=ARCHIVED + cancelled_at IS NOT NULL → status=CANCELLED, closure_status=ARCHIVED
       (preserves the cancelled-then-archived history)

  status=ARCHIVED + cancelled_at IS NULL     → status=VALIDATED, closure_status=ARCHIVED
       (the most common archive path was VALIDATED→ARCHIVED or
       DELIVERED→ARCHIVED; both fold to status=VALIDATED + closure ARCHIVED)
"""
from django.db import migrations, models


def backfill_closure(apps, schema_editor):
    AnalysisRequest = apps.get_model('analysis_requests', 'AnalysisRequest')

    # Two updates, one for each legacy workflow value. Done as bulk UPDATEs
    # to keep the migration fast even on large tables.
    AnalysisRequest.objects.filter(status='DELIVERED').update(
        status='VALIDATED',
        closure_status='DELIVERED',
    )
    AnalysisRequest.objects.filter(status='ARCHIVED', cancelled_at__isnull=False).update(
        status='CANCELLED',
        closure_status='ARCHIVED',
    )
    AnalysisRequest.objects.filter(status='ARCHIVED', cancelled_at__isnull=True).update(
        status='VALIDATED',
        closure_status='ARCHIVED',
    )


def reverse_backfill(apps, schema_editor):
    """Best-effort reverse: requests with closure DELIVERED/ARCHIVED get their
    workflow status rewound to the legacy single-axis value. Not perfectly
    lossless (we cannot recover a CANCELLED→ARCHIVED row's original
    pre-cancel workflow), but matches the original split rule on the way
    out so a forward-then-backward cycle is consistent."""
    AnalysisRequest = apps.get_model('analysis_requests', 'AnalysisRequest')
    AnalysisRequest.objects.filter(closure_status='DELIVERED').update(status='DELIVERED')
    AnalysisRequest.objects.filter(closure_status='ARCHIVED').update(status='ARCHIVED')


WORKFLOW_CHOICES = [
    ('DRAFT', 'Draft'),
    ('CONFIRMED', 'Confirmed'),
    ('COLLECTION_IN_PROGRESS', 'Collection In Progress'),
    ('IN_ANALYSIS', 'In Analysis'),
    ('AWAITING_REVIEW', 'Awaiting Review'),
    ('RETEST_REQUIRED', 'Retest Required'),
    ('READY_FOR_RELEASE', 'Ready For Release'),
    ('VALIDATED', 'Validated'),
    ('IN_PROGRESS', 'In Progress'),
    ('COMPLETED', 'Completed'),
    ('CANCELLED', 'Cancelled'),
]

CLOSURE_CHOICES = [
    ('OPEN', 'Open'),
    ('DELIVERED', 'Delivered'),
    ('ARCHIVED', 'Archived'),
]


class Migration(migrations.Migration):

    dependencies = [
        ('analysis_requests', '0017_analysisrequest_archived_at_and_more'),
    ]

    operations = [
        # 1) Add the new closure_status column, defaulting every existing row to OPEN.
        migrations.AddField(
            model_name='analysisrequest',
            name='closure_status',
            field=models.CharField(
                choices=CLOSURE_CHOICES,
                default='OPEN',
                db_index=True,
                max_length=15,
            ),
        ),
        # 2) Backfill — see module docstring for the rules.
        migrations.RunPython(backfill_closure, reverse_backfill),
        # 3) Tighten the workflow status choices: DELIVERED/ARCHIVED can no
        #    longer appear there. Pure choices change — no DB schema mutation
        #    on PostgreSQL since CharField stores arbitrary strings.
        migrations.AlterField(
            model_name='analysisrequest',
            name='status',
            field=models.CharField(
                choices=WORKFLOW_CHOICES,
                db_index=True,
                default='DRAFT',
                max_length=25,
            ),
        ),
    ]
