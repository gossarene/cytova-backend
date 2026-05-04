"""
Phase 2 — LabelSequence schema swap from ``(year, month)`` to
``period_key``.

What's pinned here
------------------
- The new ``period_key`` field exists, is unique within a tenant
  schema, and accepts both shapes the allocator will eventually
  emit (``"YYYY-MM"`` for monthly, ``"YYYY"`` for yearly).
- The ``period_key_for`` helper still emits monthly keys (Phase 2
  hardcodes monthly to keep external behaviour identical).
- The legacy ``year`` / ``month`` columns are gone — accessing them
  raises ``AttributeError``, proving the migration dropped them.
- The allocator's external surface (barcode format + monotonic
  increment) is bit-for-bit identical to the pre-Phase-2 behaviour.
  ``test_label_codes.py`` already pins this and is expected to keep
  passing without modification — Phase 2 deliberately doesn't touch
  it; this file covers what the schema swap proves on its own.
"""
from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, transaction

from apps.requests.label_service import period_key_for
from apps.requests.models import LabelSequence


# ---------------------------------------------------------------------------
# Helper — period_key_for emits the canonical monthly format
# ---------------------------------------------------------------------------

class TestPeriodKeyHelper:

    def test_monthly_format_zero_pads_month(self):
        """Single-digit months must zero-pad so the period_key sorts
        lexicographically by date — important if an audit reader
        ever scans the table by ``ORDER BY period_key``."""
        assert period_key_for(date(2026, 1, 1)) == '2026-01'
        assert period_key_for(date(2026, 9, 30)) == '2026-09'

    def test_monthly_format_zero_pads_year(self):
        """Defensive: a hypothetical year < 1000 (test fixture
        accident, future-far date) still emits a 4-digit year so the
        format width stays stable."""
        assert period_key_for(date(842, 7, 1)) == '0842-07'

    def test_distinct_months_emit_distinct_keys(self):
        """Sanity that the helper is a bijection over (year, month)
        — required for the unique constraint to do its job."""
        keys = {period_key_for(date(2026, m, 1)) for m in range(1, 13)}
        assert len(keys) == 12


# ---------------------------------------------------------------------------
# Model — period_key shape, uniqueness, defaults
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLabelSequenceShape:

    def test_can_create_row_with_monthly_key(self):
        seq = LabelSequence.objects.create(period_key='2026-04')
        seq.refresh_from_db()
        assert seq.period_key == '2026-04'
        assert seq.last_value == 0  # default

    def test_can_create_row_with_yearly_key(self):
        """Phase 3 wires the yearly-reset path. The schema must
        accept the ``"YYYY"`` shape today so Phase 3 is purely a
        service-layer change."""
        seq = LabelSequence.objects.create(period_key='2027')
        seq.refresh_from_db()
        assert seq.period_key == '2027'

    def test_period_key_is_unique(self):
        """The unique constraint is the load-bearing invariant — the
        allocator's get_or_create relies on it. Two rows with the
        same key would let two concurrent transactions race past
        ``select_for_update``, producing duplicate barcodes."""
        LabelSequence.objects.create(period_key='2026-04')
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LabelSequence.objects.create(period_key='2026-04')

    def test_year_and_month_columns_are_gone(self):
        """Pin the migration's intent: the legacy columns must NOT
        be reachable through the model. A future refactor that
        re-adds them would resurrect a dead lookup path."""
        seq = LabelSequence.objects.create(period_key='2026-04')
        assert not hasattr(seq, 'year')
        assert not hasattr(seq, 'month')

    def test_str_repr_uses_period_key(self):
        """Operators read ``__str__`` from the Django admin and from
        log lines. The new repr must surface the period_key so an
        on-call engineer can correlate a sequence row to a barcode."""
        seq = LabelSequence.objects.create(period_key='2026-04', last_value=42)
        assert str(seq) == 'LabelSequence(2026-04 @ 42)'
