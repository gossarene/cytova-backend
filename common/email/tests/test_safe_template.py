"""
Phase 1 — ``common.email.safe_template`` unit tests.

Pure-function coverage. No Django DB, no model fixtures. Two surfaces
are pinned:

  - ``find_disallowed_variables`` — the validator's primitive. Drives
    the field-level error in ``LabSettingsUpdateSerializer``.
  - ``render_safe_notification_template`` — the runtime renderer.
    Operator-customisable templates flow through this exactly once
    per email send.

The two-layer safety model (validator + renderer both reference the
same allow-list) means that even if a bad template somehow lands in
the database, the renderer cannot substitute medical content into
it. These tests pin both layers separately so a refactor that
weakens one is caught immediately.
"""
from __future__ import annotations

from common.email.safe_template import (
    PATIENT_NOTIFICATION_ALLOWED_VARS,
    find_disallowed_variables,
    render_safe_notification_template,
)


# ---------------------------------------------------------------------------
# Allow-list shape — pinned to catch accidental widening
# ---------------------------------------------------------------------------

class TestAllowListShape:

    def test_exactly_four_variables_allowed(self):
        """Adding to this set is a deliberate model decision — every
        variable here flows into a tenant-customisable email body
        and must clear the confidentiality review documented in the
        module docstring. Pin the exact set so a future PR cannot
        widen it without updating this test."""
        assert PATIENT_NOTIFICATION_ALLOWED_VARS == frozenset({
            'patient_first_name',
            'lab_name',
            'result_link',
            'request_reference',
        })

    def test_obvious_medical_variables_not_in_allow_list(self):
        """Defensive: spot-check that the categories of variables
        spec §2 explicitly forbids are NOT in the allow-list. If
        any of these names ever appear in the set, the renderer
        becomes a confidentiality breach."""
        forbidden_categories = {
            'result_value', 'exam_name', 'diagnosis',
            'is_abnormal', 'date_of_birth', 'patient_phone',
            'patient_email', 'pdf_password', 'access_token',
        }
        leaks = forbidden_categories & PATIENT_NOTIFICATION_ALLOWED_VARS
        assert leaks == set(), f'forbidden vars in allow-list: {leaks}'


# ---------------------------------------------------------------------------
# find_disallowed_variables — save-time validator
# ---------------------------------------------------------------------------

class TestFindDisallowedVariables:

    def test_empty_template_returns_empty_list(self):
        """Spec §5 fallback path: empty templates are valid (the
        renderer treats them as "use default"). Validator must not
        refuse them."""
        assert find_disallowed_variables('') == []

    def test_template_using_only_allowed_vars_returns_empty(self):
        template = (
            'Hello {{ patient_first_name }} from {{ lab_name }}, '
            'request {{ request_reference }} — {{ result_link }}.'
        )
        assert find_disallowed_variables(template) == []

    def test_single_disallowed_variable_returned(self):
        template = 'Your glucose result is {{ result_value }}.'
        assert find_disallowed_variables(template) == ['result_value']

    def test_multiple_disallowed_variables_returned_sorted_distinct(self):
        """Validator must return sorted distinct names so the
        admin-form error message is deterministic + every offender
        surfaces (operator can fix all of them in one pass)."""
        template = (
            'DOB: {{ date_of_birth }}, '
            'result: {{ result_value }}, '
            'pwd: {{ pdf_password }}, '
            '(again: {{ result_value }})'
        )
        assert find_disallowed_variables(template) == [
            'date_of_birth', 'pdf_password', 'result_value',
        ]

    def test_mixed_allowed_and_disallowed_returns_only_disallowed(self):
        """The allowed names in the same template don't show up in
        the error — operator only sees what they need to fix."""
        template = (
            'Hi {{ patient_first_name }}, '
            'your DOB {{ date_of_birth }} is on file.'
        )
        assert find_disallowed_variables(template) == ['date_of_birth']

    def test_optional_whitespace_format_recognised(self):
        """``{{var}}`` / ``{{ var }}`` / ``{{  var  }}`` all parse
        as the same placeholder — the regex tolerates any internal
        whitespace. Disallowed names match in every spacing
        variant."""
        for spacing in ('{{result_value}}', '{{ result_value }}', '{{  result_value  }}'):
            assert find_disallowed_variables(f'Foo {spacing} bar') == ['result_value']

    def test_jinja_filter_syntax_does_not_match_as_placeholder(self):
        """``{{ var | upper }}`` is rejected by the regex (filter
        pipe makes the inner content fail the identifier pattern).
        It's left in the template body and renders verbatim — no
        Jinja behaviour is silently honoured."""
        # No "var" key in the disallowed list because the regex
        # didn't match it as a placeholder at all.
        assert find_disallowed_variables('Hi {{ patient_first_name | upper }}') == []

    def test_attribute_access_does_not_match_as_placeholder(self):
        """``{{ var.attr }}`` is similarly rejected — the dot makes
        the inner content fail the identifier pattern. Defends
        against operators trying to climb attribute chains."""
        assert find_disallowed_variables('Hi {{ patient.first_name }}') == []


# ---------------------------------------------------------------------------
# render_safe_notification_template — runtime renderer
# ---------------------------------------------------------------------------

class TestRenderSafeNotificationTemplate:

    def test_substitutes_allowed_placeholder(self):
        out = render_safe_notification_template(
            'Hello {{ patient_first_name }}',
            {'patient_first_name': 'Ada'},
        )
        assert out == 'Hello Ada'

    def test_substitutes_all_four_allowed_placeholders(self):
        template = (
            'Hi {{ patient_first_name }} — '
            'lab: {{ lab_name }}, '
            'ref: {{ request_reference }}, '
            'link: {{ result_link }}'
        )
        out = render_safe_notification_template(template, {
            'patient_first_name': 'Ada',
            'lab_name': 'Acme Lab',
            'request_reference': 'REQ-0001',
            'result_link': 'https://example.test/r/abc',
        })
        assert out == (
            'Hi Ada — lab: Acme Lab, ref: REQ-0001, '
            'link: https://example.test/r/abc'
        )

    def test_unknown_placeholder_left_unchanged(self):
        """Visible-typo principle (module docstring + validated
        decision #1): a placeholder the renderer doesn't recognise
        stays literally in the output so the operator sees it in
        the preview and self-corrects. The validator catches this
        at save time on the standard path; the renderer's behaviour
        is the safety net."""
        out = render_safe_notification_template(
            'Hi {{ patient_first_name }}, your value is {{ result_value }}.',
            {'patient_first_name': 'Ada'},
        )
        assert out == 'Hi Ada, your value is {{ result_value }}.'

    def test_missing_context_value_renders_as_empty_string(self):
        """Spec invariant: render-time NEVER raises. A caller that
        forgot to populate one allowed variable gets a clean
        empty-slot output instead of leaking ``"None"`` or crashing
        the email send."""
        out = render_safe_notification_template(
            'Hi {{ patient_first_name }}, lab {{ lab_name }}.',
            {'patient_first_name': 'Ada'},  # lab_name omitted
        )
        assert out == 'Hi Ada, lab .'

    def test_none_context_value_renders_as_empty_string(self):
        """Same defensive rule for explicit ``None`` — the renderer
        coerces to '' rather than the literal string 'None'."""
        out = render_safe_notification_template(
            'Hi {{ patient_first_name }}',
            {'patient_first_name': None},
        )
        assert out == 'Hi '

    def test_empty_template_returns_empty_string(self):
        """Spec §5 fallback path partner to the validator: empty
        template → empty render. The Phase 2 service layer treats
        this as the "use hard-coded default" signal."""
        assert render_safe_notification_template('', {}) == ''

    def test_optional_whitespace_around_placeholder_name(self):
        """All three spacing variants substitute identically."""
        for spacing in ('{{patient_first_name}}', '{{ patient_first_name }}', '{{  patient_first_name  }}'):
            out = render_safe_notification_template(
                f'Hi {spacing}',
                {'patient_first_name': 'Ada'},
            )
            assert out == 'Hi Ada'

    def test_no_html_escape_by_default(self):
        """Plain-text body path: the renderer leaves angle brackets
        and ampersands raw. The text body is delivered verbatim;
        no encoding is needed."""
        out = render_safe_notification_template(
            'Hi {{ patient_first_name }}',
            {'patient_first_name': '<Ada & Co>'},
        )
        assert out == 'Hi <Ada & Co>'

    def test_html_escape_when_requested(self):
        """HTML body path: substituted values get ``html.escape``'d
        so an operator-typed value like ``"O'Brien"`` or a name
        with ``<`` characters can't break the surrounding markup
        or open an XSS hole."""
        out = render_safe_notification_template(
            'Hi {{ patient_first_name }}',
            {'patient_first_name': '<Ada & Co>'},
            escape_html=True,
        )
        assert out == 'Hi &lt;Ada &amp; Co&gt;'

    def test_html_escape_quotes_for_attribute_safety(self):
        """``escape_html=True`` uses ``quote=True`` so values can
        be safely interpolated into an HTML attribute as well as
        into element text. Quote characters become ``&quot;`` /
        ``&#x27;``."""
        out = render_safe_notification_template(
            '{{ patient_first_name }}',
            {'patient_first_name': 'O\'Brien'},
            escape_html=True,
        )
        assert '&#x27;' in out or '&apos;' in out

    def test_no_jinja_filter_execution(self):
        """``{{ var | upper }}`` MUST NOT pipe through any filter
        — the regex doesn't match it as a placeholder and the
        whole expression renders literally. Pinned because the
        whole point of the safe renderer is to refuse Jinja
        semantics."""
        out = render_safe_notification_template(
            '{{ patient_first_name | upper }}',
            {'patient_first_name': 'Ada'},
        )
        assert out == '{{ patient_first_name | upper }}'

    def test_no_attribute_access_execution(self):
        """``{{ obj.attr }}`` similarly renders literally — the dot
        makes the regex skip over it. Defends against operators
        trying to climb attribute chains to reach forbidden
        fields."""
        out = render_safe_notification_template(
            '{{ patient.first_name }}',
            {'patient': {'first_name': 'Ada'}},  # ignored
        )
        assert out == '{{ patient.first_name }}'

    def test_no_call_syntax_execution(self):
        """``{{ func() }}`` is similarly inert."""
        out = render_safe_notification_template(
            '{{ format() }}',
            {'format': lambda: 'EXEC'},  # ignored
        )
        assert out == '{{ format() }}'


# ---------------------------------------------------------------------------
# End-to-end safety property — medical content can NEVER reach output
# ---------------------------------------------------------------------------

class TestConfidentialityProperty:

    _MEDICAL_VOCAB = (
        # Numbers + units that signal a value
        'mg/dL', 'mmol/L', '12.5', 'positive', 'negative',
        # Diagnosis-style terms
        'diabetes', 'anemia', 'infection',
        # Identity beyond first_name
        'date of birth', 'phone number',
    )

    def test_template_with_only_disallowed_vars_yields_no_substitution(self):
        """Even if a malicious actor uses templates containing the
        names of forbidden variables, the renderer leaves them
        literal — the medical *value* never enters the output."""
        template = (
            'Hi {{ patient_first_name }}, '
            'your value is {{ result_value }} mg/dL '
            'and your DOB is {{ date_of_birth }}.'
        )
        # Even if a context maliciously provides values for the
        # forbidden names, the renderer ignores them.
        out = render_safe_notification_template(template, {
            'patient_first_name': 'Ada',
            'result_value': '12.5',  # would-be leak — ignored
            'date_of_birth': '1985-03-14',  # would-be leak — ignored
        })
        # The substituted name renders. The forbidden-name
        # placeholders are left literal.
        assert out == (
            'Hi Ada, '
            'your value is {{ result_value }} mg/dL '
            'and your DOB is {{ date_of_birth }}.'
        )
        # The actual value never appears in the output, even
        # though it was passed in the context.
        assert '12.5' not in out
        assert '1985-03-14' not in out

    def test_validator_rejects_all_obvious_medical_attempts(self):
        """Defence-in-depth: the validator refuses the same
        templates at save time. Operators get a clear error rather
        than silently shipping a template with literal placeholders
        in patient emails."""
        attempts = [
            'Your result is {{ result_value }} mg/dL.',
            'Your DOB is {{ date_of_birth }}.',
            'Password: {{ pdf_password }}.',
            'Diagnosis: {{ diagnosis }}.',
            'Token: {{ access_token }}.',
            'Phone: {{ patient_phone }}.',
        ]
        for tmpl in attempts:
            bad = find_disallowed_variables(tmpl)
            assert len(bad) == 1, f'expected 1 disallowed in {tmpl!r}, got {bad}'
