"""
Phase 1 — Reference-range parsing + abnormal-flag computation.

Pure-function tests for ``apps.results.abnormal_detection``. No
Django DB access, no model fixtures — every test is a string-in,
value-out assertion against the helpers.

Coverage map (every spec §6 edge case is pinned)
------------------------------------------------
  numeric below min                    → True
  numeric above max                    → True
  numeric inside range                 → False
  numeric on boundary                  → False (inclusive)
  comma decimal "5,6"                  → parsed as 5.6
  bare "<5" / ">100"                   → one-sided bounds
  "<=5" / "≤5" / ">=5" / "≥5"          → same as bare, inclusive
  "5 to 15"                            → parsed as (5, 15)
  en-dash "12.0–16.0"                  → parsed
  em-dash "12.0—16.0"                  → parsed
  trailing unit "5-15 mg/dL"           → range parsed, unit ignored
  "Positive" / "Negative"              → indeterminate (None)
  empty value / empty range            → indeterminate
  non-numeric value "abc"              → indeterminate
  min > max in input                   → indeterminate (safe fallback)
  whitespace tolerance                 → trimmed
  NaN / inf                            → rejected
"""
from __future__ import annotations

from apps.results.abnormal_detection import (
    _parse_numeric,
    compute_abnormal_from_reference,
    parse_reference_range,
)


# ---------------------------------------------------------------------------
# _parse_numeric — value-side parser
# ---------------------------------------------------------------------------

class TestParseNumeric:

    def test_integer_value(self):
        assert _parse_numeric('5') == 5.0

    def test_decimal_value_dot(self):
        assert _parse_numeric('5.6') == 5.6

    def test_decimal_value_comma(self):
        """Spec §2 invariant: comma decimals must parse as floats.
        The original string stays untouched in the model — this
        helper is comparison-side only."""
        assert _parse_numeric('5,6') == 5.6

    def test_negative_value(self):
        assert _parse_numeric('-3.5') == -3.5

    def test_explicit_positive_sign(self):
        assert _parse_numeric('+3.5') == 3.5

    def test_whitespace_is_trimmed(self):
        assert _parse_numeric('  5.6  ') == 5.6

    def test_empty_string_returns_none(self):
        """Spec §2: ``"if value is empty → do not auto-change"``.
        The helper communicates "can't parse" via ``None``."""
        assert _parse_numeric('') is None

    def test_pure_text_returns_none(self):
        assert _parse_numeric('Positive') is None
        assert _parse_numeric('abc') is None

    def test_value_with_inequality_returns_none(self):
        """Operators sometimes type ``"<5"`` directly into the
        value field instead of leaving it numeric. We refuse to
        auto-decide — the rule applies to numeric values only."""
        assert _parse_numeric('<5') is None
        assert _parse_numeric('>100') is None

    def test_value_with_unit_suffix_returns_none(self):
        """Same logic — ``"5 mg/dL"`` is not a clean numeric. The
        operator entered the unit by mistake; refuse to auto-decide
        rather than guess."""
        assert _parse_numeric('5 mg/dL') is None

    def test_nan_and_inf_rejected(self):
        """Float() will happily parse these special tokens but they
        produce nonsense comparisons later. Refuse."""
        assert _parse_numeric('nan') is None
        assert _parse_numeric('inf') is None
        assert _parse_numeric('-inf') is None


# ---------------------------------------------------------------------------
# parse_reference_range — range-side parser
# ---------------------------------------------------------------------------

class TestParseReferenceRangeTwoSided:

    def test_simple_dash_range(self):
        assert parse_reference_range('70-100') == (70.0, 100.0)

    def test_decimal_dash_range(self):
        assert parse_reference_range('12.0-16.0') == (12.0, 16.0)

    def test_en_dash_range(self):
        """En-dash (U+2013) is the canonical typographic hyphen in
        many European medical fixtures. Fixtures in this repo
        already use it: see catalog migrations."""
        assert parse_reference_range('12.0–16.0') == (12.0, 16.0)

    def test_em_dash_range(self):
        """Em-dash (U+2014) shows up as a typo for en-dash. Accept
        it — the operator's intent is unambiguous."""
        assert parse_reference_range('12.0—16.0') == (12.0, 16.0)

    def test_comma_decimal_range(self):
        """European fixtures often combine comma decimals + dash.
        The parser treats them as a single coherent shape."""
        assert parse_reference_range('12,0-16,0') == (12.0, 16.0)

    def test_whitespace_around_dash(self):
        """Operators paste from external systems that pad the
        operator with spaces. Be forgiving."""
        assert parse_reference_range('5 - 15') == (5.0, 15.0)

    def test_to_keyword_range(self):
        """English-language style some imports emit. Case-insensitive."""
        assert parse_reference_range('5 to 15') == (5.0, 15.0)
        assert parse_reference_range('5 TO 15') == (5.0, 15.0)

    def test_trailing_unit_is_ignored(self):
        """Spec §6 implicit case — fixtures like ``"5-15 mg/dL"``
        carry the unit as trailing text. The parser anchors to a
        word boundary after the second number, so trailing text
        is silently dropped."""
        assert parse_reference_range('5-15 mg/dL') == (5.0, 15.0)


class TestParseReferenceRangeOneSided:

    def test_bare_lt_upper_bound(self):
        """``"<5"`` is interpreted as max=5 (inclusive). The
        inclusivity simplification is documented in the module
        docstring — operator can manually toggle if the boundary
        case matters clinically."""
        assert parse_reference_range('<5') == (None, 5.0)

    def test_bare_gt_lower_bound(self):
        assert parse_reference_range('>100') == (100.0, None)

    def test_le_upper_bound(self):
        assert parse_reference_range('<=5') == (None, 5.0)

    def test_ge_lower_bound(self):
        assert parse_reference_range('>=100') == (100.0, None)

    def test_unicode_le_upper_bound(self):
        """U+2264 ``≤`` — emitted by some EMRs / lab instruments
        instead of the ASCII two-character form."""
        assert parse_reference_range('≤5') == (None, 5.0)

    def test_unicode_ge_lower_bound(self):
        """U+2265 ``≥``."""
        assert parse_reference_range('≥100') == (100.0, None)

    def test_two_char_op_takes_precedence_over_single_char(self):
        """Critical regex-precedence test: ``"<=5"`` must NOT be
        mis-parsed as ``"<"`` followed by ``"=5"``. Pin it
        explicitly so a future regex refactor can't reintroduce
        this class of bug."""
        assert parse_reference_range('<=5') == (None, 5.0)
        assert parse_reference_range('>=5') == (5.0, None)


class TestParseReferenceRangeIndeterminate:

    def test_empty_returns_none_pair(self):
        """Spec §2: ``"if no reference range exists → do not
        auto-change abnormal"``. The helper signals that with a
        ``(None, None)`` return."""
        assert parse_reference_range('') == (None, None)

    def test_qualitative_text_returns_none_pair(self):
        """``"Positive"`` / ``"Negative"`` are qualitative results
        — there's no numeric range to compare against."""
        assert parse_reference_range('Positive') == (None, None)
        assert parse_reference_range('Negative') == (None, None)
        assert parse_reference_range('Not detected') == (None, None)

    def test_unrecognised_separator_returns_none_pair(self):
        """``"5..15"`` and ``"5 / 10"`` are not patterns we
        promise to handle. Refuse rather than guess."""
        assert parse_reference_range('5..15') == (None, None)
        assert parse_reference_range('5 / 10') == (None, None)

    def test_min_greater_than_max_returns_none_pair(self):
        """Spec §6 ``"safe fallback"`` — operator data-entry
        error. The parser refuses to auto-flag against a
        nonsensical bound; the operator has to fix the catalog
        entry first."""
        assert parse_reference_range('15-5') == (None, None)
        assert parse_reference_range('100 to 5') == (None, None)


# ---------------------------------------------------------------------------
# compute_abnormal_from_reference — the integration helper
# ---------------------------------------------------------------------------

class TestComputeAbnormalNumericRange:

    def test_value_below_min_is_abnormal(self):
        assert compute_abnormal_from_reference('5', '10-20') is True

    def test_value_above_max_is_abnormal(self):
        assert compute_abnormal_from_reference('25', '10-20') is True

    def test_value_inside_range_is_normal(self):
        assert compute_abnormal_from_reference('15', '10-20') is False

    def test_value_on_lower_boundary_is_normal(self):
        """Spec §2: ``"min <= value <= max → abnormal = false"``.
        Boundary is inclusive — ``value == min`` is normal."""
        assert compute_abnormal_from_reference('10', '10-20') is False

    def test_value_on_upper_boundary_is_normal(self):
        """Same on the upper side — ``value == max`` is normal."""
        assert compute_abnormal_from_reference('20', '10-20') is False

    def test_comma_decimal_value_works(self):
        """Full chain: comma decimal value vs comma decimal range.
        The European-fixture happy path."""
        assert compute_abnormal_from_reference('5,6', '4,0-6,0') is False
        assert compute_abnormal_from_reference('6,5', '4,0-6,0') is True
        assert compute_abnormal_from_reference('3,9', '4,0-6,0') is True


class TestComputeAbnormalOneSidedBounds:

    def test_value_above_lt_bound_is_abnormal(self):
        """Reference ``"<5"`` means ``max=5``. Anything > 5 is
        outside the normal zone."""
        assert compute_abnormal_from_reference('6', '<5') is True

    def test_value_below_lt_bound_is_normal(self):
        assert compute_abnormal_from_reference('3', '<5') is False

    def test_value_below_gt_bound_is_abnormal(self):
        """Reference ``">100"`` means ``min=100``. Anything < 100
        is outside the normal zone."""
        assert compute_abnormal_from_reference('80', '>100') is True

    def test_value_above_gt_bound_is_normal(self):
        assert compute_abnormal_from_reference('120', '>100') is False


class TestComputeAbnormalIndeterminate:

    def test_empty_value_returns_none(self):
        """Spec §2: empty value → no auto-change. The caller MUST
        leave the existing flag alone."""
        assert compute_abnormal_from_reference('', '10-20') is None

    def test_non_numeric_value_returns_none(self):
        """Spec §2: non-numeric value → no auto-change."""
        assert compute_abnormal_from_reference('Positive', '10-20') is None
        assert compute_abnormal_from_reference('<5', '10-20') is None

    def test_empty_reference_returns_none(self):
        """Spec §2: no reference range → no auto-change."""
        assert compute_abnormal_from_reference('15', '') is None

    def test_qualitative_reference_returns_none(self):
        """``"Negative"`` carries no numeric bound — refuse to
        decide rather than guess."""
        assert compute_abnormal_from_reference('15', 'Negative') is None

    def test_unparseable_reference_returns_none(self):
        assert compute_abnormal_from_reference('15', '5..15') is None

    def test_min_greater_than_max_reference_returns_none(self):
        """Spec §6 ``"safe fallback"`` — refuse to auto-flag
        against a nonsensical bound. If we don't, a typo'd catalog
        entry would silently flag every result as abnormal."""
        assert compute_abnormal_from_reference('15', '20-10') is None


class TestComputeAbnormalIntegrationCornerCases:

    def test_only_min_via_ge_outer_value_normal(self):
        """``">=4"`` → ``min=4``, no upper bound. Anything >= 4
        is normal regardless of how large."""
        assert compute_abnormal_from_reference('1000000', '>=4') is False

    def test_only_min_via_ge_lower_value_abnormal(self):
        assert compute_abnormal_from_reference('3', '>=4') is True

    def test_only_max_via_le_outer_value_abnormal(self):
        """``"<=5"`` → ``max=5``, no lower bound. Anything > 5 is
        abnormal regardless of how small."""
        assert compute_abnormal_from_reference('-1000000', '<=5') is False
        assert compute_abnormal_from_reference('5.0001', '<=5') is True

    def test_unicode_bound_works_end_to_end(self):
        """Spot-check that the ``≤`` / ``≥`` Unicode forms flow
        through compute_abnormal correctly — not just the parser."""
        assert compute_abnormal_from_reference('6', '≤5') is True
        assert compute_abnormal_from_reference('80', '≥100') is True

    def test_trailing_unit_does_not_break_decision(self):
        """Real-world fixture: ``"5-15 mg/dL"`` reference, value
        ``"7"``. Should decide normal."""
        assert compute_abnormal_from_reference('7', '5-15 mg/dL') is False
        assert compute_abnormal_from_reference('20', '5-15 mg/dL') is True
