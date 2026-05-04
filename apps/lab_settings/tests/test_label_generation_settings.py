"""
Phase 1 — LabSettings fields for the flexible-labels rollout.

Pure model + serializer coverage. Phases 2–4 will wire these fields
into ``LabelSequence`` / ``RequestLabelService``; Phase 1's contract
is purely "the fields exist, defaults preserve current behaviour,
validators reject bad input, the serializer round-trips them."

Why the defaults matter
-----------------------
The pre-rollout label generator hard-codes ``EXTRA_LABELS_BONUS = 2``
and uses a per-(year, month) sequence. The strict-rules guarantee
"do not break existing behaviour" forces every default here to
reproduce that exact baseline:

    label_numbering_mode         = PER_FAMILY
    extra_label_count            = 2
    label_sequence_reset_period  = MONTHLY

These tests pin those defaults so a future migration that
accidentally changes them gets caught at CI time, not in production.
"""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.lab_settings.models import (
    LabelNumberingMode, LabelSequenceResetPeriod, LabSettings,
)
from apps.lab_settings.serializers import (
    LabSettingsSerializer, LabSettingsUpdateSerializer,
)


# ---------------------------------------------------------------------------
# Defaults — must reproduce current behaviour exactly
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLabelGenerationDefaults:

    def test_freshly_created_settings_carry_back_compat_defaults(self):
        """A fresh ``LabSettings.get_solo()`` row must hand back the
        exact triple that reproduces today's hard-coded behaviour:
        PER_FAMILY mode, 2 extras, monthly reset. Phases 2-4 read
        these fields verbatim — drift here would silently change
        every existing tenant's barcode behaviour."""
        s = LabSettings.get_solo()
        assert s.label_numbering_mode == LabelNumberingMode.PER_FAMILY
        assert s.extra_label_count == 2
        assert s.label_sequence_reset_period == LabelSequenceResetPeriod.MONTHLY


# ---------------------------------------------------------------------------
# extra_label_count — bounds + type
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestExtraLabelCountBounds:

    def test_zero_is_allowed(self):
        """Zero extras is the documented "minimal labels" config —
        the field must accept it without complaint."""
        s = LabSettings.get_solo()
        s.extra_label_count = 0
        s.full_clean()  # raises if invalid

    def test_negative_value_is_rejected(self):
        """The MinValueValidator(0) must refuse negatives at
        ``full_clean`` time so admin-side updates that try to send a
        negative number get a clean ValidationError instead of a
        silent DB-level failure (PositiveSmallIntegerField bounds
        differ across backends)."""
        s = LabSettings.get_solo()
        s.extra_label_count = -1
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        # The error must point at the right field so an admin form
        # can attach it to the right input.
        assert 'extra_label_count' in exc.value.error_dict

    def test_large_value_is_allowed(self):
        """Some operational scenarios (training labs, label-printer
        QA runs) legitimately want many extras. Sanity-check the
        upper-bound is the field's native limit (PositiveSmallInteger
        max ~32k), not a tighter validator."""
        s = LabSettings.get_solo()
        s.extra_label_count = 100
        s.full_clean()
        s.save(update_fields=['extra_label_count', 'updated_at'])
        s.refresh_from_db()
        assert s.extra_label_count == 100


# ---------------------------------------------------------------------------
# Choices — TextChoices reject unknown values
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChoiceValidation:

    def test_label_numbering_mode_rejects_unknown_value(self):
        """An admin form posting a typo'd mode must fail closed.
        The TextChoices set is the canonical authority — adding a
        new mode is a deliberate model + migration change, not an
        accidental string write."""
        s = LabSettings.get_solo()
        s.label_numbering_mode = 'PER_TUBE'  # not a valid choice
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        assert 'label_numbering_mode' in exc.value.error_dict

    def test_label_numbering_mode_accepts_same_request_number(self):
        s = LabSettings.get_solo()
        s.label_numbering_mode = LabelNumberingMode.SAME_REQUEST_NUMBER
        s.full_clean()
        s.save(update_fields=['label_numbering_mode', 'updated_at'])
        s.refresh_from_db()
        assert s.label_numbering_mode == 'SAME_REQUEST_NUMBER'

    def test_reset_period_rejects_unknown_value(self):
        s = LabSettings.get_solo()
        s.label_sequence_reset_period = 'WEEKLY'
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        assert 'label_sequence_reset_period' in exc.value.error_dict

    def test_reset_period_accepts_yearly(self):
        s = LabSettings.get_solo()
        s.label_sequence_reset_period = LabelSequenceResetPeriod.YEARLY
        s.full_clean()
        s.save(update_fields=['label_sequence_reset_period', 'updated_at'])
        s.refresh_from_db()
        assert s.label_sequence_reset_period == 'YEARLY'


# ---------------------------------------------------------------------------
# Choice-class shape — pin the canonical strings
# ---------------------------------------------------------------------------

class TestChoiceEnumShape:

    def test_label_numbering_mode_has_exactly_two_values(self):
        """The phased rollout consumes these enum values literally
        (Phase 4 branches the allocator on them). Drift here would
        either make Phase 4 unreachable or reach a code path the
        spec doesn't cover. Pin the set."""
        values = {c.value for c in LabelNumberingMode}
        assert values == {'PER_FAMILY', 'SAME_REQUEST_NUMBER'}

    def test_reset_period_has_exactly_two_values(self):
        values = {c.value for c in LabelSequenceResetPeriod}
        assert values == {'MONTHLY', 'YEARLY'}


# ---------------------------------------------------------------------------
# Serializer round-trip — read + update both expose the new fields
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSerializerRoundTrip:

    def test_read_serializer_exposes_new_fields(self):
        """Even before the label service consumes the settings,
        ops + the admin UI need to read them via GET /lab-settings/.
        Validates the fields are present in the read serializer's
        output and carry the back-compat defaults."""
        s = LabSettings.get_solo()
        data = LabSettingsSerializer(s).data
        assert data['label_numbering_mode'] == 'PER_FAMILY'
        assert data['extra_label_count'] == 2
        assert data['label_sequence_reset_period'] == 'MONTHLY'

    def test_update_serializer_accepts_partial_changes(self):
        """A PATCH that flips only ``extra_label_count`` must
        succeed without forcing the caller to repeat every other
        unrelated field — the existing UpdateSerializer is partial,
        and our additions inherit that contract."""
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'extra_label_count': 5,
                'label_numbering_mode': 'SAME_REQUEST_NUMBER',
                'label_sequence_reset_period': 'YEARLY',
            },
            partial=True,
        )
        assert ser.is_valid(), ser.errors
        ser.save()

        s.refresh_from_db()
        assert s.extra_label_count == 5
        assert s.label_numbering_mode == 'SAME_REQUEST_NUMBER'
        assert s.label_sequence_reset_period == 'YEARLY'

    def test_update_serializer_rejects_negative_extras(self):
        """The MinValueValidator on the model must surface through
        the serializer — bad input never reaches save()."""
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s, data={'extra_label_count': -3}, partial=True,
        )
        assert not ser.is_valid()
        assert 'extra_label_count' in ser.errors

    def test_update_serializer_rejects_unknown_choice(self):
        """Bad enum values must be rejected at serializer time so the
        admin form sees a structured field error, not a 500 at
        save."""
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s, data={'label_numbering_mode': 'PER_TUBE'}, partial=True,
        )
        assert not ser.is_valid()
        assert 'label_numbering_mode' in ser.errors
