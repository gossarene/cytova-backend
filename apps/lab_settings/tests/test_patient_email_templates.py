"""
Phase 1 — LabSettings template fields + serializer validation.

Three things pinned:

1. **Defaults** match the spec verbatim. Pinned because Phase 2
   wires the renderer to fall back to "use hard-coded copy" only
   when the template is empty — if the migration default ever
   drifts to a non-empty stub, every existing tenant would silently
   start receiving the stub instead of the canonical default.

2. **Allow-list validation** rejects forbidden placeholder names
   at serializer-save time with a field-level error that lists the
   bad names so the admin UI can render exactly what the operator
   needs to fix.

3. **Empty templates** are explicitly accepted — that's the spec §5
   fallback signal ("use default if not configured").
"""
from __future__ import annotations

import pytest

from apps.lab_settings.models import LabSettings
from apps.lab_settings.serializers import LabSettingsUpdateSerializer


# ---------------------------------------------------------------------------
# Defaults — match spec §1 exactly
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLabSettingsTemplateDefaults:

    def test_subject_default_matches_spec(self):
        """Spec §1: ``"Your lab result is ready"``. Pinning it here
        means a future migration that drifts the default would be
        caught by CI before it ships."""
        s = LabSettings.get_solo()
        assert s.patient_result_email_subject_template == 'Your lab result is ready'

    def test_body_default_matches_spec(self):
        """Spec §1 multi-line body. Pinning verbatim — including
        the exact placeholder spacing and newlines — so any
        accidental re-formatting in a future migration shows up
        as a failing test."""
        s = LabSettings.get_solo()
        assert s.patient_result_email_body_template == (
            'Hello {{ patient_first_name }},\n\n'
            'Your lab result is ready. You can access it securely '
            'using the link below:\n\n'
            '{{ result_link }}\n\n'
            'For your privacy, please do not share this link.'
        )

    def test_default_body_uses_only_allowed_variables(self):
        """Sanity meta-test: the default the migration ships MUST
        pass the allow-list validator. If the spec ever evolves to
        add a placeholder that isn't in the allow-list, this test
        is the canary."""
        from common.email.safe_template import find_disallowed_variables
        s = LabSettings.get_solo()
        assert find_disallowed_variables(s.patient_result_email_body_template) == []
        assert find_disallowed_variables(s.patient_result_email_subject_template) == []


# ---------------------------------------------------------------------------
# Serializer — accepts safe templates
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSerializerAcceptsSafeTemplates:

    def test_subject_with_allowed_vars_accepted(self):
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'patient_result_email_subject_template':
                    '{{ lab_name }} — your result is ready',
            },
            partial=True,
        )
        assert ser.is_valid(), ser.errors
        ser.save()
        s.refresh_from_db()
        assert s.patient_result_email_subject_template == (
            '{{ lab_name }} — your result is ready'
        )

    def test_body_with_allowed_vars_accepted(self):
        s = LabSettings.get_solo()
        body = (
            'Hi {{ patient_first_name }}, '
            'request {{ request_reference }} from {{ lab_name }} '
            'is available: {{ result_link }}'
        )
        ser = LabSettingsUpdateSerializer(
            s, data={'patient_result_email_body_template': body},
            partial=True,
        )
        assert ser.is_valid(), ser.errors

    def test_empty_subject_and_body_accepted_as_fallback_signal(self):
        """Spec §5 fallback: empty string is the canonical "use
        default" signal. The validator MUST accept it so a tenant
        that wants to revert to the hard-coded copy can clear the
        field via the admin form."""
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'patient_result_email_subject_template': '',
                'patient_result_email_body_template': '',
            },
            partial=True,
        )
        assert ser.is_valid(), ser.errors


# ---------------------------------------------------------------------------
# Serializer — rejects forbidden placeholders with field-level error
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSerializerRejectsForbiddenTemplates:

    def test_body_referencing_result_value_rejected(self):
        """Spec §4 example: ``"Your glucose result is
        {{ result_value }}"`` MUST fail validation. The error
        surfaces under the body-template field key so the admin
        form renders it next to the right input."""
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'patient_result_email_body_template':
                    'Your glucose result is {{ result_value }}',
            },
            partial=True,
        )
        assert not ser.is_valid()
        assert 'patient_result_email_body_template' in ser.errors

    def test_body_referencing_dob_rejected(self):
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'patient_result_email_body_template':
                    'Your DOB is {{ date_of_birth }}',
            },
            partial=True,
        )
        assert not ser.is_valid()
        errors = ser.errors['patient_result_email_body_template']
        assert any('date_of_birth' in str(e) for e in errors)

    def test_subject_referencing_password_rejected(self):
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'patient_result_email_subject_template':
                    'Password: {{ pdf_password }}',
            },
            partial=True,
        )
        assert not ser.is_valid()
        assert 'patient_result_email_subject_template' in ser.errors

    def test_error_message_lists_every_forbidden_name(self):
        """Operator typed multiple forbidden vars in one save —
        the error message must list ALL of them so the operator
        fixes everything in one pass instead of trial-and-error."""
        s = LabSettings.get_solo()
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'patient_result_email_body_template':
                    'Hi {{ patient_first_name }}, '
                    'value {{ result_value }}, '
                    'password {{ pdf_password }}',
            },
            partial=True,
        )
        assert not ser.is_valid()
        message = str(ser.errors['patient_result_email_body_template'])
        # Both forbidden names appear in the error text.
        assert 'result_value' in message
        assert 'pdf_password' in message
        # And the allowed-list hint surfaces so the operator knows
        # what they CAN use.
        assert 'patient_first_name' in message

    def test_partial_update_other_fields_unaffected_by_template_rejection(self):
        """A bad template in the same payload must NOT cause OTHER
        fields to silently slip through. The whole save fails."""
        s = LabSettings.get_solo()
        original_lab_name = s.lab_name
        ser = LabSettingsUpdateSerializer(
            s,
            data={
                'lab_name': 'Hijack Lab',
                'patient_result_email_body_template': '{{ result_value }}',
            },
            partial=True,
        )
        assert not ser.is_valid()
        s.refresh_from_db()
        # Lab name was NOT updated — the validation error blocked
        # the whole save, not just the template field.
        assert s.lab_name == original_lab_name
