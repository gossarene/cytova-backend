"""
Cytova — Patient Portal token cleanup.

Periodic housekeeping for the ``PatientOutstandingToken`` table:
delete rows whose ``expires_at`` has passed. The corresponding
``PatientBlacklistedToken`` rows cascade-delete via the OneToOne
``on_delete=CASCADE``, so a single DELETE keeps the two tables in
sync.

Run modes
---------
- ``python manage.py cleanup_patient_tokens``  — delete expired rows
- ``python manage.py cleanup_patient_tokens --dry-run`` — count only

Recommended schedule: daily, after low-traffic hours. The query is
indexed on ``expires_at`` so it's cheap to run even when the table is
large.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.patient_portal.models import PatientOutstandingToken


class Command(BaseCommand):
    help = (
        'Delete expired PatientOutstandingToken rows. Cascades to the '
        'corresponding PatientBlacklistedToken rows.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report how many rows would be deleted without writing.',
        )

    def handle(self, *args, **options):
        cutoff = timezone.now()
        qs = PatientOutstandingToken.objects.filter(expires_at__lte=cutoff)
        count = qs.count()

        if options['dry_run']:
            self.stdout.write(
                self.style.NOTICE(
                    f'[dry-run] would delete {count} expired patient '
                    f'outstanding token row(s) (expires_at <= {cutoff:%Y-%m-%d %H:%M:%S})'
                )
            )
            return

        if count == 0:
            self.stdout.write('No expired patient tokens to clean up.')
            return

        deleted, breakdown = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted} row(s) across {len(breakdown)} table(s) '
            f'(cutoff: {cutoff:%Y-%m-%d %H:%M:%S}).'
        ))
        for table, n in breakdown.items():
            self.stdout.write(f'  - {table}: {n}')
