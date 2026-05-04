"""
Cytova — Safe placeholder substitution for operator-customisable
email templates.

Pure-function module. No Django, no Jinja2, no template engine of any
kind — just a regex that recognises ``{{ variable }}`` placeholders
and looks them up in a plain dict. The whole point is that the
operator's text passes through ``str.format``-free / eval-free so
nothing they paste can become executable.

Two-layer safety contract
-------------------------
The system runs the same allow-list check at two distinct moments,
and that's deliberate:

  1. **Save time** — ``find_disallowed_variables`` is called from
     ``LabSettingsUpdateSerializer``. A template that references any
     variable outside ``PATIENT_NOTIFICATION_ALLOWED_VARS`` is
     refused with a field-level ``ValidationError`` listing the
     offending names. Operators cannot ship a bad template.

  2. **Render time** — ``render_safe_notification_template`` itself
     only substitutes the allow-listed names. Any leftover
     placeholder (typo, future migration, manual DB edit) is left
     **unchanged in the output** so the typo is visible in the
     preview and the operator self-corrects. Removing it silently
     would either swallow whitespace/punctuation around the
     placeholder OR mask the bug.

The two layers together guarantee that — no matter how a template
arrives in the database — medical / sensitive data CANNOT be
substituted into a patient email. The allow-list is a tiny set of
identity + link strings only.

Why no Jinja
------------
Jinja2 supports filters, conditionals, function calls, attribute
access. In a tenant-customisable surface that's a sandbox-escape
attack surface waiting to happen. The product requirement is "string
substitution with four named slots" — anything more is unnecessary
power that has to be guarded against.
"""
from __future__ import annotations

import html
import re
from typing import Iterable, Mapping


# Allow-list of placeholders the operator may use in patient-result
# notification emails. Adding to this set is a deliberate model
# change — every variable here flows into a tenant-customisable
# email body, so each addition must be reviewed for confidentiality
# (no medical content) and for delivery safety (no raw tokens, no
# password hashes, etc.).
#
# The four below are exactly what spec §2 authorises:
#   - patient_first_name : already in the email envelope's "to"
#     name; surfacing it in the body adds no new privacy risk.
#   - lab_name           : the lab's own public identity.
#   - result_link        : the secure access URL. Already the only
#     load-bearing piece of the existing hard-coded body.
#   - request_reference  : the patient-facing public reference the
#     operator quotes on receipts. Already public.
#
# Explicitly NOT allowed (forbidden by spec §2): any exam name,
# any result value, any diagnosis hint, any abnormal flag, any
# patient identity beyond first_name (DOB / phone / email), the
# raw access token, the PDF password.
PATIENT_NOTIFICATION_ALLOWED_VARS: frozenset[str] = frozenset({
    'patient_first_name',
    'lab_name',
    'result_link',
    'request_reference',
})


# Regex matching a Jinja-style placeholder ``{{ variable }}`` with
# optional surrounding whitespace inside the braces. The variable
# name is captured for both the validator and the renderer.
#
#   ``{{var}}``        ✓
#   ``{{ var }}``      ✓
#   ``{{  var  }}``    ✓
#   ``{{ var | up }}`` ✗ (filter pipe makes the name not match
#                          ``[\w]+\s*}}``, treated as plain text)
#
# Variable names follow Python identifier conventions (letters,
# digits, underscore; cannot start with a digit). Anything outside
# that — e.g. ``{{ var.attr }}``, ``{{ var() }}`` — fails to match
# and is therefore left in the output verbatim, never executed.
_PLACEHOLDER_RE = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}')


def find_disallowed_variables(
    template: str,
    allowed: Iterable[str] = PATIENT_NOTIFICATION_ALLOWED_VARS,
) -> list[str]:
    """Return the sorted distinct list of placeholder names referenced
    in ``template`` that are NOT in ``allowed``.

    Empty or all-allowed templates return ``[]`` — the caller treats
    that as "safe to save". A non-empty return is the validator's
    field-level error payload: the operator sees exactly which
    forbidden names they typed.

    Pure function — does not touch the model, does not raise. The
    caller decides how to surface the result (serializer
    ``ValidationError``, admin form, preview UI).
    """
    if not template:
        return []
    allowed_set = set(allowed)
    found = {
        match.group(1)
        for match in _PLACEHOLDER_RE.finditer(template)
    }
    return sorted(found - allowed_set)


def render_safe_notification_template(
    template: str,
    context: Mapping[str, str],
    *,
    escape_html: bool = False,
) -> str:
    """Substitute allow-listed placeholders with values from ``context``.

    Behaviour
    ---------
    - Only names present in ``PATIENT_NOTIFICATION_ALLOWED_VARS`` are
      considered for substitution. Unknown placeholders are left
      **unchanged** in the output (visible-typo principle — see
      module docstring).
    - Names in the allow-list but missing from ``context`` render as
      the empty string, NOT as ``None`` or the literal placeholder.
      Defensive: a caller that forgot to populate one variable gets
      a clean output instead of leaking ``"None"`` into a patient
      email.
    - When ``escape_html=True`` every substituted value is HTML-
      escaped via ``html.escape``. Use this for the HTML body. The
      plain-text body should pass ``escape_html=False``.

    Why no ``KeyError``
    -------------------
    Email rendering happens at send time, often inside a Celery task
    or a hot HTTP path. Raising on a missing context key would turn
    a missing dict entry into a delivery failure for the patient.
    Returning empty-string keeps the email shippable and surfaces
    the bug only as a visibly empty slot in the rendered text.

    Why ``escape_html`` is keyword-only
    -----------------------------------
    The two destinations have very different escape semantics. A
    positional arg makes it easy to pass the wrong polarity by
    accident; keyword-only forces the caller to read the rule.
    """
    if not template:
        return ''
    allowed_set = PATIENT_NOTIFICATION_ALLOWED_VARS

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in allowed_set:
            # Unknown placeholder — leave the original ``{{ ... }}``
            # in place so a typo is visible in the preview. The
            # validator caught this at save time for the standard
            # path; this branch only fires on manually-edited or
            # legacy templates.
            return match.group(0)
        raw_value = context.get(name, '')
        if raw_value is None:
            raw_value = ''
        if not isinstance(raw_value, str):
            raw_value = str(raw_value)
        if escape_html:
            return html.escape(raw_value, quote=True)
        return raw_value

    return _PLACEHOLDER_RE.sub(_replace, template)
