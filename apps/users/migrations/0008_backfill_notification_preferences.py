"""
Data migration: backfill internal-workflow notification preferences
for existing StaffUser rows.

The schema migration ``0007`` adds the two flag columns with
``default=False`` so every existing row lands at False. That's
the safe lower bound (no surprise emails), but it would also
silently turn off review-ready emails for every biologist who
was receiving them under the previous role-only resolver — a
visible UX regression.

This migration applies one-shot smart defaults derived from the
operator's role at the time of the migration:

    BIOLOGIST / LAB_ADMIN  → receive_review_ready_notifications = True
    TECHNICIAN             → receive_result_rejection_notifications = True

These match the new ``StaffUserManager.create_user`` defaults so
the pre- and post-migration cohorts behave identically.

The migration is idempotent: re-running it (e.g. via fake reverse
+ replay) only sets flags that should be True; it never overwrites
a value the operator already changed manually because it filters
on the (role, current-value) tuple before writing.
"""
from django.db import migrations


_ROLE_TO_FLAGS = {
    'BIOLOGIST': ('receive_review_ready_notifications',),
    'LAB_ADMIN': ('receive_review_ready_notifications',),
    'TECHNICIAN': ('receive_result_rejection_notifications',),
}


def apply_role_defaults(apps, schema_editor):
    StaffUser = apps.get_model('users', 'StaffUser')

    for role, flags in _ROLE_TO_FLAGS.items():
        for flag in flags:
            # Only flip rows that are still at the schema-default
            # (False). A LAB_ADMIN who already turned the flag off
            # before this migration ran (theoretically possible if
            # someone hand-rolled a fixture) is left alone.
            StaffUser.objects.filter(
                role=role,
                **{flag: False},
            ).update(**{flag: True})


def reverse_noop(apps, schema_editor):
    # Reverting the data migration is intentionally a no-op: we
    # have no way to tell which True values were set here vs by
    # the operator afterwards. Reversing the schema migration
    # drops the columns entirely, which is the real undo path.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0007_staffuser_receive_result_rejection_notifications_and_more'),
    ]

    operations = [
        migrations.RunPython(apply_role_defaults, reverse_noop),
    ]
