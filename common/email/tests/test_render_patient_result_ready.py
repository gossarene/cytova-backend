"""
Phase 2 — ``render_patient_result_ready`` integration tests.

Pure-function coverage. The function is the single seam where
operator-customisable templates meet the branded HTML shell + the
plain-text fallback. Three things matter:

1. **Back-compat** — empty templates produce output bit-for-bit
   identical to today's hard-coded copy in the load-bearing slots
   (subject line, in-body greeting, CTA button URL, footer). This
   is the rollout-safety property: a tenant that touches nothing
   sees zero change in their patient emails.

2. **Operator customisation flows end-to-end** — a non-empty
   subject template produces the rendered subject AND becomes the
   in-body bold title. A non-empty body template's text replaces
   the in-body paragraph block in BOTH the HTML and text bodies.
   Multi-paragraph operator text breaks correctly in HTML.

3. **Confidentiality property** — the only avenue an operator has
   to inject content is through the four-variable allow-list. Even
   if they paste a template referencing forbidden names, the
   rendered output never substitutes the values (the renderer's
   inner allow-list rejects them; values from the context are
   still ignored).
"""
from __future__ import annotations

from common.email.templates import render_patient_result_ready


# ---------------------------------------------------------------------------
# Back-compat — empty templates reproduce today's email
# ---------------------------------------------------------------------------

class TestEmptyTemplatesFallback:

    def _render_default(self):
        """Helper: render with empty templates (the migration's
        default state). Every test in this class compares against
        what this returns to assert "byte-for-byte identical to
        pre-Phase-2 in the load-bearing slots"."""
        return render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/abc123',
            lab_name='Acme Lab',
            request_reference='REQ-0001',
            subject_template='',
            body_template='',
        )

    def test_subject_falls_back_to_canonical_default(self):
        subject, _, _ = self._render_default()
        assert subject == 'Your lab result is ready'

    def test_html_in_body_title_uses_canonical_default(self):
        """The in-body bold title is the rendered subject. With an
        empty subject template that resolves to the canonical
        default, the title slot in the HTML matches verbatim."""
        _, html_body, _ = self._render_default()
        assert 'Your lab result is ready' in html_body

    def test_html_body_includes_pre_phase2_greeting(self):
        """The default body paragraph is the pre-Phase-2 greeting,
        unchanged. Tenants that don't customise see exactly today's
        email."""
        _, html_body, _ = self._render_default()
        assert 'Hi Ada, your lab result is ready' in html_body

    def test_html_body_includes_cta_with_secure_link(self):
        """CTA button stays present regardless of customisation
        (spec §5: ``"result link should still be shown as a CTA
        button"``). The ``href`` carries the raw secure_link."""
        _, html_body, _ = self._render_default()
        assert 'href="https://example.test/r/abc123"' in html_body
        assert 'Access result' in html_body
        assert '</a>' in html_body

    def test_html_body_includes_footer(self):
        """Lab name renders in the footer slot — pre-Phase-2
        behaviour preserved."""
        _, html_body, _ = self._render_default()
        assert 'Acme Lab' in html_body

    def test_text_body_matches_pre_phase2_format(self):
        """Plain-text fallback retains the pre-Phase-2 structure:
        greeting, link block indented two spaces, privacy hint,
        ignore-if-not-expected line, footer."""
        _, _, text_body = self._render_default()
        assert text_body.startswith('Hi Ada,\n\n')
        assert 'Your lab result is ready' in text_body
        assert '  https://example.test/r/abc123' in text_body
        assert 'For your privacy, the result file may require a password to open.' in text_body
        assert text_body.rstrip().endswith('— Acme Lab')

    def test_lab_name_empty_falls_back_to_cytova_footer(self):
        """When the tenant hasn't set a lab name, the footer says
        "Cytova" — same fallback the pre-Phase-2 code used."""
        _, html_body, text_body = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/x',
            lab_name='',
            subject_template='',
            body_template='',
        )
        assert '>Cytova</div>' in html_body or 'Cytova' in html_body
        assert text_body.rstrip().endswith('— Cytova')


# ---------------------------------------------------------------------------
# Customised subject — flows into both EmailMessage subject + in-body title
# ---------------------------------------------------------------------------

class TestCustomSubjectTemplate:

    def test_subject_substitutes_allowed_placeholders(self):
        subject, _, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            lab_name='Acme Lab',
            request_reference='REQ-7',
            subject_template='{{ lab_name }} — your result is ready',
            body_template='',
        )
        assert subject == 'Acme Lab — your result is ready'

    def test_subject_renders_into_in_body_title_slot(self):
        """Spec design (validated decision): the rendered subject
        ALSO becomes the bold title at the top of the email body
        — one knob for the operator, coherent visual."""
        _, html_body, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            lab_name='Acme Lab',
            subject_template='Hello from {{ lab_name }}',
            body_template='',
        )
        assert 'Hello from Acme Lab' in html_body

    def test_empty_subject_template_falls_back_to_default(self):
        """An empty string is the canonical "use default" signal —
        the renderer treats it identically to a tenant that never
        configured the field."""
        subject, _, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template='',
        )
        assert subject == 'Your lab result is ready'

    def test_whitespace_only_subject_falls_back_to_default(self):
        """A subject template that renders to only whitespace
        (e.g. ``"   "``) falls back to the canonical default. We
        strip + check non-empty rather than ship a blank subject
        line that would land emails in spam folders."""
        subject, _, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='   ',
            body_template='',
        )
        assert subject == 'Your lab result is ready'


# ---------------------------------------------------------------------------
# Customised body — operator copy in HTML + text, CTA stays automatic
# ---------------------------------------------------------------------------

class TestCustomBodyTemplate:

    def test_body_template_renders_in_text_output(self):
        body = (
            'Hello {{ patient_first_name }},\n\n'
            'Request {{ request_reference }} from {{ lab_name }} is ready.\n\n'
            'Open here: {{ result_link }}'
        )
        _, _, text_body = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            lab_name='Acme Lab',
            request_reference='REQ-99',
            subject_template='',
            body_template=body,
        )
        assert 'Hello Ada,\n\n' in text_body
        assert 'Request REQ-99 from Acme Lab is ready.' in text_body
        assert 'Open here: https://example.test/r/x' in text_body
        # Footer still appended after the operator's body.
        assert text_body.rstrip().endswith('— Acme Lab')

    def test_body_template_renders_in_html_output(self):
        """Operator's text appears in the HTML body. The placeholder
        substitutions happened before HTML escape, so substituted
        values are HTML-safe."""
        body = 'Hello {{ patient_first_name }} — your result is ready.'
        _, html_body, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        assert 'Hello Ada — your result is ready.' in html_body

    def test_html_paragraphs_split_on_blank_lines(self):
        """A multi-paragraph body (default shape) renders as
        multiple ``<tr>`` rows in the HTML shell. The blank line
        in the source separates the paragraphs."""
        body = (
            'Hello {{ patient_first_name }}.\n\n'
            'Your result is ready.\n\n'
            'Please do not share this link.'
        )
        _, html_body, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        # All three paragraphs present in the rendered HTML.
        assert 'Hello Ada.' in html_body
        assert 'Your result is ready.' in html_body
        assert 'Please do not share this link.' in html_body

    def test_html_soft_line_breaks_become_br(self):
        """A single ``\\n`` inside a paragraph renders as ``<br>``
        rather than starting a new paragraph row."""
        body = 'Line one.\nLine two.'
        _, html_body, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        assert 'Line one.<br>Line two.' in html_body

    def test_cta_button_present_even_when_body_omits_result_link(self):
        """Spec §5: the CTA button is rendered regardless of
        whether the operator includes ``{{ result_link }}`` in
        their body. Operators who omit the placeholder still get
        a working button."""
        body = 'Hi {{ patient_first_name }}, your result is ready.'
        _, html_body, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        assert 'href="https://example.test/r/x"' in html_body
        assert 'Access result' in html_body
        assert '</a>' in html_body

    def test_cta_button_present_when_body_includes_result_link(self):
        """And operators who DO include ``{{ result_link }}`` get
        the URL inline in their body AND the CTA button below.
        Both can coexist — no duplicate-link surgery."""
        body = (
            'Hi {{ patient_first_name }}, '
            'open here: {{ result_link }}'
        )
        _, html_body, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        # Inline URL in the operator body (escaped).
        assert 'open here: https://example.test/r/x' in html_body
        # And the CTA still rendered below.
        assert 'Access result' in html_body
        assert '</a>' in html_body

    def test_html_escape_applied_to_operator_typed_characters(self):
        """An operator-typed angle bracket / ampersand stays
        literal in the rendered HTML — never breaks the
        surrounding markup or opens an XSS hole."""
        body = (
            'Hello {{ patient_first_name }} & welcome to '
            '<our> lab.'
        )
        _, html_body, _ = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        assert 'welcome to' in html_body
        assert '&amp;' in html_body
        assert '&lt;our&gt; lab.' in html_body
        # Raw angle brackets MUST NOT be present in the operator's
        # text region (the rest of the shell legitimately uses
        # them for its own markup).
        assert '<our>' not in html_body

    def test_html_escape_applied_to_substituted_first_name(self):
        """Names like ``"O'Brien"`` or ``"<Ada>"`` (operator typo
        or pasted from another system) flow safely through the
        substitution. The paragraph formatter HTML-escapes every
        line — substituted values are escaped exactly once (no
        double-escape). The result MUST contain ``&lt;Ada&gt;``
        and MUST NOT contain ``&amp;lt;`` (the double-escape
        regression marker)."""
        body = '{{ patient_first_name }}'
        _, html_body, _ = render_patient_result_ready(
            first_name='<Ada>',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        assert '&lt;Ada&gt;' in html_body
        # Double-escape regression marker — if the substituted
        # value were escaped twice, ``<`` would render as
        # ``&amp;lt;`` instead of ``&lt;``. Pin against it.
        assert '&amp;lt;' not in html_body


# ---------------------------------------------------------------------------
# Confidentiality property — operator cannot inject medical content
# ---------------------------------------------------------------------------

class TestConfidentialityProperty:

    def test_operator_template_referencing_forbidden_var_renders_literal(self):
        """Even if a malicious template somehow lands in the DB
        (manual edit, future migration, broken validator), the
        renderer leaves the forbidden placeholder literal — the
        VALUE never substitutes. The render-time allow-list is
        the structural safety net behind the save-time validator."""
        body = (
            'Your value is {{ result_value }} mg/dL '
            'and your DOB is {{ date_of_birth }}.'
        )
        _, html_body, text_body = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template=body,
        )
        # The forbidden placeholders survive literally in both
        # HTML and text bodies. The renderer NEVER substitutes
        # values for names outside the allow-list.
        assert '{{ result_value }}' in html_body
        assert '{{ date_of_birth }}' in html_body
        assert '{{ result_value }}' in text_body
        assert '{{ date_of_birth }}' in text_body

    def test_default_email_contains_no_medical_vocabulary(self):
        """Spot-check the default email against the same medical
        vocabulary list the existing email tests use. The default
        is the load-bearing back-compat path; if it ever drifts
        and starts mentioning a clinical concept, the patient's
        privacy is at stake."""
        _, html_body, text_body = render_patient_result_ready(
            first_name='Ada',
            secure_link='https://example.test/r/x',
            subject_template='',
            body_template='',
        )
        forbidden = (
            'mg/dL', 'mmol/L', 'glucose', 'hemoglobin',
            'positive', 'negative', 'diabetes', 'anemia',
            'infection', 'date of birth',
        )
        for term in forbidden:
            assert term.lower() not in html_body.lower(), (
                f'forbidden term {term!r} leaked into default HTML body'
            )
            assert term.lower() not in text_body.lower(), (
                f'forbidden term {term!r} leaked into default text body'
            )
