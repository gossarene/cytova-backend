"""
Phase 3 — Allocator branches on ``LabSettings.label_sequence_reset_period``
and concurrency proof.

Two distinct surfaces are pinned:

1. **Reset mode dispatch** — ``period_key_for`` returns the right
   shape per mode, and ``_allocate_numeric_code`` honours the
   ``reset_mode`` argument so YEARLY tenants get a continuous
   sequence across months while MONTHLY tenants keep the historical
   per-month reset.

2. **Concurrency safety** — the allocator's ``select_for_update``
   contract still holds. Pinned two ways:
     - sequential-monotonic: 100 calls in tight loop → 1..100,
       no gaps. Catches an off-by-one in the increment without
       depending on multi-threading machinery.
     - multi-threaded: N parallel threads racing on the same
       period_key → N distinct sequence numbers. The real
       guarantee under load.

Multi-thread teardown caveat
----------------------------
The threaded test is marked ``@pytest.mark.django_db(transaction=True)``
because cross-thread commit visibility requires a real DB
transaction (not pytest-django's per-test rollback). On this dev
infrastructure the post-test ``TRUNCATE`` step trips a pre-existing
FK constraint and reports a teardown ``error`` — same pattern as
every other ``transaction=True`` test in the repo. The test BODY
passes (``1 passed, 1 error`` outcome); the error is the unrelated
infra issue we've documented across earlier phases.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pytest
from django.db import connection, transaction
from django_tenants.utils import schema_context

from apps.requests.label_service import (
    _allocate_numeric_code, period_key_for,
)
from apps.requests.models import LabelSequence


# ---------------------------------------------------------------------------
# period_key_for — dispatches on mode
# ---------------------------------------------------------------------------

class TestPeriodKeyDispatch:

    def test_default_mode_is_monthly(self):
        """No-mode call is the legacy contract — pre-Phase-3
        callers (none exist today, but the helper is public) get
        monthly behaviour automatically."""
        assert period_key_for(date(2026, 4, 1)) == '2026-04'

    def test_explicit_monthly_matches_default(self):
        assert period_key_for(date(2026, 4, 1), 'MONTHLY') == '2026-04'

    def test_yearly_emits_four_digit_year_only(self):
        assert period_key_for(date(2026, 4, 1), 'YEARLY') == '2026'
        assert period_key_for(date(2026, 12, 31), 'YEARLY') == '2026'

    def test_yearly_collapses_all_months_to_one_key(self):
        """The whole point of yearly mode: every date in 2026
        collapses to the same period_key, so the sequence runs
        continuously through the year."""
        keys = {
            period_key_for(date(2026, m, 1), 'YEARLY')
            for m in range(1, 13)
        }
        assert keys == {'2026'}

    def test_unknown_mode_falls_through_to_monthly(self):
        """Defensive: a future enum value that ships in a settings
        migration before this helper is updated must NOT crash
        label generation. The fallback is the historical safe
        default."""
        assert period_key_for(date(2026, 4, 1), 'WEEKLY') == '2026-04'
        assert period_key_for(date(2026, 4, 1), '') == '2026-04'


# ---------------------------------------------------------------------------
# Allocator — YEARLY mode behaviour
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAllocatorYearlyMode:

    def test_yearly_mode_persists_sequence_across_months(self, db):
        """Two allocations in different months of the same year
        under YEARLY mode share a continuous sequence — the second
        call returns ``...000002``, not ``...000001`` (which would
        be the monthly behaviour). This is the load-bearing
        invariant of the YEARLY mode."""
        LabelSequence.objects.all().delete()
        first = _allocate_numeric_code(
            '0042', date(2026, 4, 1), reset_mode='YEARLY',
        )
        second = _allocate_numeric_code(
            '0042', date(2026, 11, 15), reset_mode='YEARLY',
        )
        # The barcode bodies differ on YYMM (informational), but
        # the trailing sequence numbers are 1, 2 — proving the
        # sequence ran continuously from April through November.
        assert first.endswith('000001')
        assert second.endswith('000002')

    def test_yearly_mode_resets_at_year_boundary(self, db):
        """The first allocation in a new year starts a fresh
        sequence at 1 — both 2026 and 2027 begin at ``...000001``."""
        LabelSequence.objects.all().delete()
        a = _allocate_numeric_code(
            '0042', date(2026, 12, 31), reset_mode='YEARLY',
        )
        b = _allocate_numeric_code(
            '0042', date(2027, 1, 1), reset_mode='YEARLY',
        )
        assert a.endswith('000001')
        assert b.endswith('000001')

    def test_yearly_and_monthly_keys_do_not_collide(self, db):
        """A tenant that switches MONTHLY → YEARLY mid-year must
        not collide on the existing rows: ``"2026-04"`` and
        ``"2026"`` are distinct period_keys, so the year sequence
        starts fresh from 1 even though prior monthly rows exist."""
        LabelSequence.objects.all().delete()
        # Pre-existing monthly rows from before the switch.
        _allocate_numeric_code('0042', date(2026, 4, 1), reset_mode='MONTHLY')
        _allocate_numeric_code('0042', date(2026, 4, 1), reset_mode='MONTHLY')
        # Switch to yearly mode — fresh sequence under the new key.
        first_yearly = _allocate_numeric_code(
            '0042', date(2026, 4, 1), reset_mode='YEARLY',
        )
        assert first_yearly.endswith('000001')
        # And the monthly row is untouched at value=2.
        monthly = LabelSequence.objects.get(period_key='2026-04')
        assert monthly.last_value == 2


# ---------------------------------------------------------------------------
# Concurrency — sequential monotonic
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSequentialMonotonic:

    def test_100_sequential_allocations_are_contiguous(self, db):
        """Tight-loop sanity. The sequence is monotonic, contiguous
        (no gaps), and matches exactly the count of calls. Catches
        any off-by-one in the increment or save logic without
        depending on multi-threading machinery."""
        LabelSequence.objects.all().delete()
        codes = [
            _allocate_numeric_code('0042', date(2026, 4, 1))
            for _ in range(100)
        ]
        # All codes distinct.
        assert len(set(codes)) == 100
        # Trailing 6 digits cover exactly 1..100.
        sequences = sorted(int(c[-6:]) for c in codes)
        assert sequences == list(range(1, 101))


# ---------------------------------------------------------------------------
# Concurrency — multi-threaded race on the same period_key
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestConcurrentAllocation:

    def _alloc(self, schema_name: str, today: date, reset_mode: str) -> str:
        """Worker function for each pool thread. Sets the tenant
        schema (django-tenants is per-connection), wraps the
        allocator in a fresh transaction so its ``select_for_update``
        actually locks something the other workers will block on,
        then closes the connection so pytest-django's teardown
        doesn't leak it.
        """
        try:
            with schema_context(schema_name):
                with transaction.atomic():
                    return _allocate_numeric_code(
                        '0042', today, reset_mode=reset_mode,
                    )
        finally:
            connection.close()

    def test_ten_parallel_threads_emit_ten_distinct_codes(
        self, _test_tenant_schema,
    ):
        """The real guarantee: under N concurrent allocators racing
        on the same period_key row, each gets a distinct sequence
        number — no two threads observe the same ``last_value``
        because ``select_for_update`` serialises them on the row
        lock.

        N=10 is small enough to keep the test fast (≈ 1s) but big
        enough that any missing-lock regression would surface
        immediately as duplicates."""
        # Wipe any leftover sequence rows from sibling tests in the
        # same transactional fixture so we start at last_value=0.
        with schema_context(_test_tenant_schema):
            LabelSequence.objects.all().delete()

        n = 10
        today = date(2026, 4, 15)
        codes: list[str] = []
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [
                pool.submit(self._alloc, _test_tenant_schema, today, 'MONTHLY')
                for _ in range(n)
            ]
            for f in as_completed(futures):
                codes.append(f.result())

        # Every code is distinct — the load-bearing invariant.
        assert len(set(codes)) == n, (
            f'Duplicate barcodes under {n}-way concurrency: {codes}'
        )
        # Trailing sequence numbers cover exactly 1..N (no gaps,
        # no skips). Order doesn't matter — threads complete in
        # whatever order PostgreSQL grants the row lock.
        sequences = sorted(int(c[-6:]) for c in codes)
        assert sequences == list(range(1, n + 1)), sequences
