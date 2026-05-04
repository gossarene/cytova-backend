"""
Cytova — Reference-range parsing and abnormal-flag detection.

Pure-function module (no model imports, no Django dependencies). The
helpers exist as a single source of truth shared by:

  - the result entry UI (frontend mirrors the parser logic — see
    Phase 2 of the auto-abnormal rollout);
  - imports / instrument-result ingestion (Phase 3 — same helper,
    no UI form involved);
  - any future bulk-revalidation tool that needs to recompute the
    flag on existing rows without re-rendering the form.

The "best-effort" caveat
------------------------
Reference ranges are stored as freeform strings on every model
(``reference_range`` / ``reference_range_snapshot``). Examples seen
in production fixtures: ``"70-100"``, ``"12.0-16.0"``,
``"12.0–16.0"`` (en-dash), ``"<5"``, ``"Positive"``,
``"5-15 mg/dL"``. A handful of common patterns parse cleanly; the
rest fall through to "indeterminate" and the caller leaves the
abnormal flag untouched. The contract is intentionally conservative:
**we never auto-change the flag when we can't be sure**.

Inclusivity simplification
--------------------------
The spec rule is ``min <= value <= max → normal``. We treat ``<X``
and ``>X`` as inclusive bounds (``max=X`` and ``min=X`` respectively)
because storing the strict-vs-inclusive distinction would balloon
the return shape and the operator can always toggle manually.
``<=X`` / ``≤X`` / ``>=X`` / ``≥X`` parse identically — same
inclusive treatment. The spec calls this out as acceptable
("best-effort" parser scope, validated decision #1 of Phase 1).
"""
from __future__ import annotations

import re
from typing import Optional

# Hyphen-like characters we accept as a range separator. Real-world
# reference-range strings vary widely by data source and language.
# Listing them explicitly makes the parser's intent obvious and
# prevents a regex-engine subtlety from silently dropping one.
#
# - ``-``  ASCII hyphen-minus  (U+002D)
# - ``–``  en-dash             (U+2013)  — common in European fixtures
# - ``—``  em-dash             (U+2014)  — typo'd from en-dash
_DASH_CHARS = '-–—'
_DASH_PATTERN = f'[{_DASH_CHARS}]'

# A single decimal number, comma OR dot decimal separator. The
# integer / fractional parts are captured together so the caller
# can normalise the comma to a dot before float parsing. Optional
# leading sign so ``-3.5`` and ``+3.5`` both parse.
_NUMBER_PATTERN = r'[+-]?\d+(?:[.,]\d+)?'

# Compiled patterns — once at import time so the helpers are cheap
# enough to call inside a per-row UI debounce loop.
_RE_NUMBER_ONLY = re.compile(rf'^\s*({_NUMBER_PATTERN})\s*$')
_RE_RANGE = re.compile(
    rf'^\s*({_NUMBER_PATTERN})\s*{_DASH_PATTERN}\s*({_NUMBER_PATTERN})\b'
)
_RE_RANGE_TO = re.compile(
    rf'^\s*({_NUMBER_PATTERN})\s+to\s+({_NUMBER_PATTERN})\b',
    re.IGNORECASE,
)
# Bounded-only patterns. Order matters in the matcher: the
# two-character operators (``<=`` / ``>=``) must be tested before
# the single-character ones, otherwise ``<=5`` would be mis-parsed
# as ``<`` plus ``=5``. The Unicode ``≤`` (U+2264) and ``≥``
# (U+2265) are accepted as canonical equivalents.
_RE_LE = re.compile(rf'^\s*(?:<=|≤)\s*({_NUMBER_PATTERN})\b')
_RE_GE = re.compile(rf'^\s*(?:>=|≥)\s*({_NUMBER_PATTERN})\b')
_RE_LT = re.compile(rf'^\s*<\s*({_NUMBER_PATTERN})\b')
_RE_GT = re.compile(rf'^\s*>\s*({_NUMBER_PATTERN})\b')


def _parse_numeric(text: str) -> Optional[float]:
    """Parse a single numeric token to ``float``, or return ``None``
    when the input doesn't represent a finite number.

    Conventions
    -----------
    - Whitespace is stripped (spec §2: ``"trim spaces"``).
    - Comma decimals are accepted (spec §2: ``"5,6" → 5.6``). The
      original string is NEVER mutated; this helper is comparison-
      side only.
    - Empty / non-numeric input returns ``None`` (the caller maps
      this to "don't auto-change").
    - ``inf`` / ``nan`` are rejected — they'd produce nonsense
      comparisons later.
    """
    if not text:
        return None
    candidate = text.strip().replace(',', '.')
    match = _RE_NUMBER_ONLY.match(candidate)
    if match is None:
        return None
    try:
        result = float(match.group(1))
    except ValueError:
        return None
    if result != result or result in (float('inf'), float('-inf')):
        # NaN / infinities — refuse rather than propagate junk.
        return None
    return result


def parse_reference_range(text: str) -> tuple[Optional[float], Optional[float]]:
    """Best-effort parser for the freeform ``reference_range`` string.

    Returns ``(min, max)`` where each side is either a ``float`` or
    ``None`` (meaning "no bound on this side"). The pair is
    ``(None, None)`` when the input doesn't match any recognised
    pattern — the caller treats that as "indeterminate" and leaves
    the abnormal flag alone.

    Recognised shapes
    -----------------
    Two-sided ranges:
      ``"70-100"``        → (70.0, 100.0)
      ``"12.0–16.0"``     → (12.0, 16.0)  (en-dash)
      ``"12,0-16,0"``     → (12.0, 16.0)  (comma decimals)
      ``"5 - 15"``        → (5.0, 15.0)   (whitespace around dash)
      ``"5 to 15"``       → (5.0, 15.0)
      ``"5-15 mg/dL"``    → (5.0, 15.0)   (trailing unit ignored)

    One-sided bounds:
      ``"<5"`` / ``"<=5"`` / ``"≤5"`` → (None, 5.0)
      ``">100"`` / ``">=100"`` / ``"≥100"`` → (100.0, None)

    Indeterminate (returns ``(None, None)``):
      - Empty string.
      - Pure-text values (``"Positive"``, ``"Negative"``).
      - Patterns we don't recognise (``"5..15"``, ``"5 / 10"``).
      - Parsed pair where ``min > max`` (operator data-entry error;
        we refuse rather than auto-flag against a nonsensical
        bound — spec §6 ``"safe fallback"``).
    """
    if not text:
        return (None, None)

    # Two-sided: dash form. ``"5-15 mg/dL"`` matches because the
    # regex anchors to ``\b`` after the second number — trailing
    # text is ignored.
    match = _RE_RANGE.match(text)
    if match:
        lo = _parse_numeric(match.group(1))
        hi = _parse_numeric(match.group(2))
        return _validate_pair(lo, hi)

    # Two-sided: ``"5 to 15"`` form, English-language style some
    # imports emit. Case-insensitive so ``"5 TO 15"`` works.
    match = _RE_RANGE_TO.match(text)
    if match:
        lo = _parse_numeric(match.group(1))
        hi = _parse_numeric(match.group(2))
        return _validate_pair(lo, hi)

    # One-sided upper bound. ``<=`` and ``≤`` MUST be checked before
    # the bare ``<`` so the two-character form doesn't get
    # short-circuited.
    match = _RE_LE.match(text)
    if match:
        return (None, _parse_numeric(match.group(1)))
    match = _RE_LT.match(text)
    if match:
        return (None, _parse_numeric(match.group(1)))

    # One-sided lower bound. Same precedence rule.
    match = _RE_GE.match(text)
    if match:
        return (_parse_numeric(match.group(1)), None)
    match = _RE_GT.match(text)
    if match:
        return (_parse_numeric(match.group(1)), None)

    return (None, None)


def _validate_pair(
    lo: Optional[float], hi: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    """Apply the spec §6 ``min > max`` safe fallback. Returns
    ``(None, None)`` when the parsed pair is nonsense; otherwise
    returns the pair as-is. Either side can still be ``None`` —
    that just means parse failed on that side."""
    if lo is None or hi is None:
        return (lo, hi)
    if lo > hi:
        return (None, None)
    return (lo, hi)


def compute_abnormal_from_reference(
    value: str, reference_range_text: str,
) -> Optional[bool]:
    """Decide whether a result value should be auto-flagged abnormal.

    Returns
    -------
    - ``True``  — value parsed numerically AND falls outside the
      parsed reference range. The caller should set the abnormal
      flag.
    - ``False`` — value parsed numerically AND falls inside the
      parsed reference range. The caller should clear the abnormal
      flag.
    - ``None``  — indeterminate. The caller MUST leave the existing
      abnormal flag alone. Reasons we return ``None``:
        * empty value (spec §2: ``"if value is empty → do not
          auto-change abnormal"``),
        * non-numeric value (``"Positive"``, ``"<5"`` typed by
          mistake, etc.),
        * unparseable reference range (``"Negative"``, free text),
        * parsed range had ``min > max`` (operator data-entry
          error; refuse rather than auto-flag).

    The helper is the canonical decision point — every consumer
    (UI, import pipeline, future bulk recompute) goes through this
    same function so the rule lives in one place.
    """
    numeric_value = _parse_numeric(value)
    if numeric_value is None:
        return None

    lo, hi = parse_reference_range(reference_range_text)
    if lo is None and hi is None:
        # No usable bound. The reference string was empty,
        # qualitative, or unparseable. Don't make a decision.
        return None

    # Spec §2 comparison: outside means strictly below ``min`` OR
    # strictly above ``max``. The boundary itself is normal.
    if lo is not None and numeric_value < lo:
        return True
    if hi is not None and numeric_value > hi:
        return True
    return False
