"""Verification email templates (HTML + plain text).

Kept as Python f-string templates rather than Django templates so this
module has no side-effect on settings.TEMPLATES['DIRS'] or app discovery.
The template is small and unlikely to need designer-level edits; if that
changes, migrate to a real Django template directory.

Inputs are escaped where they're interpolated into HTML (`first_name`).
The verification code is digits only (validated upstream by the
serializer regex `^\\d{6}$`) so additional escaping is unnecessary.
"""
from __future__ import annotations

import html
from typing import Tuple


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Cytova verification code</title>
</head>
<body style="margin:0; padding:0; background:#f8fafc; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; color:#0f172a;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f8fafc; padding:40px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="480" style="max-width:480px; background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:40px;">
          <tr>
            <td align="center" style="padding-bottom:24px;">
              <span style="display:inline-block; font-size:20px; font-weight:600; letter-spacing:-0.02em; color:#2563eb;">Cytova</span>
            </td>
          </tr>
          <tr>
            <td style="font-size:18px; font-weight:600; color:#0f172a; padding-bottom:8px;">
              Verify your email
            </td>
          </tr>
          <tr>
            <td style="font-size:14px; line-height:1.6; color:#475569; padding-bottom:24px;">
              Hi {first_name}, use this code to verify your email:
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:8px 0 24px 0;">
              <div style="display:inline-block; padding:18px 28px; background:#f1f5f9; border:1px solid #e2e8f0; border-radius:12px; font-family:'SF Mono',Menlo,Consolas,monospace; font-size:32px; font-weight:600; letter-spacing:8px; color:#0f172a;">
                {code}
              </div>
            </td>
          </tr>
          <tr>
            <td style="font-size:13px; line-height:1.6; color:#64748b; padding-bottom:32px;">
              This code is valid for {expires_minutes} minutes.
            </td>
          </tr>
          <tr>
            <td style="border-top:1px solid #e2e8f0; padding-top:20px; font-size:12px; line-height:1.5; color:#94a3b8;">
              If you did not request this, you can safely ignore this email.
            </td>
          </tr>
        </table>
        <div style="font-size:11px; color:#94a3b8; padding-top:16px;">© Cytova</div>
      </td>
    </tr>
  </table>
</body>
</html>
"""


_TEXT_TEMPLATE = """\
Hi {first_name},

Use this code to verify your email:

  {code}

This code is valid for {expires_minutes} minutes.

If you did not request this, you can safely ignore this email.

— Cytova
"""


def render_verification(*, first_name: str, code: str, expires_minutes: int) -> Tuple[str, str]:
    """Return (html_body, text_body) for the verification email."""
    safe_name = (first_name or 'there').strip() or 'there'
    return (
        _HTML_TEMPLATE.format(
            first_name=html.escape(safe_name),
            code=code,
            expires_minutes=expires_minutes,
        ),
        _TEXT_TEMPLATE.format(
            first_name=safe_name,
            code=code,
            expires_minutes=expires_minutes,
        ),
    )


# ---------------------------------------------------------------------------
# Password reset
#
# Same visual vocabulary as the verification email so the brand stays
# consistent across the auth lifecycle. The reset URL is interpolated raw
# into both `href` (URL-safe by construction — the token is base64url) and
# the visible link text. Recipient name is HTML-escaped; URL is not, since
# escaping would break the link.
# ---------------------------------------------------------------------------

_RESET_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reset your Cytova password</title>
</head>
<body style="margin:0; padding:0; background:#f8fafc; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; color:#0f172a;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f8fafc; padding:40px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="480" style="max-width:480px; background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:40px;">
          <tr>
            <td align="center" style="padding-bottom:24px;">
              <span style="display:inline-block; font-size:20px; font-weight:600; letter-spacing:-0.02em; color:#2563eb;">Cytova</span>
            </td>
          </tr>
          <tr>
            <td style="font-size:18px; font-weight:600; color:#0f172a; padding-bottom:8px;">
              Reset your password
            </td>
          </tr>
          <tr>
            <td style="font-size:14px; line-height:1.6; color:#475569; padding-bottom:24px;">
              Hi {first_name}, we received a request to reset your password. Click the button below to choose a new one.
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:8px 0 24px 0;">
              <a href="{reset_url}" style="display:inline-block; padding:12px 28px; background:#2563eb; color:#ffffff; font-size:14px; font-weight:600; text-decoration:none; border-radius:10px;">
                Reset password
              </a>
            </td>
          </tr>
          <tr>
            <td style="font-size:13px; line-height:1.6; color:#64748b; padding-bottom:8px;">
              Or copy and paste this link into your browser:
            </td>
          </tr>
          <tr>
            <td style="font-size:12px; line-height:1.5; color:#2563eb; word-break:break-all; padding-bottom:24px;">
              <a href="{reset_url}" style="color:#2563eb; text-decoration:underline;">{reset_url}</a>
            </td>
          </tr>
          <tr>
            <td style="font-size:13px; line-height:1.6; color:#64748b; padding-bottom:32px;">
              This link is valid for {expires_minutes} minutes and can be used only once.
            </td>
          </tr>
          <tr>
            <td style="border-top:1px solid #e2e8f0; padding-top:20px; font-size:12px; line-height:1.5; color:#94a3b8;">
              If you did not request a password reset, you can safely ignore this email — your password will remain unchanged.
            </td>
          </tr>
        </table>
        <div style="font-size:11px; color:#94a3b8; padding-top:16px;">© Cytova</div>
      </td>
    </tr>
  </table>
</body>
</html>
"""


_RESET_TEXT_TEMPLATE = """\
Hi {first_name},

We received a request to reset your Cytova password. Open the link below to choose a new one:

  {reset_url}

This link is valid for {expires_minutes} minutes and can be used only once.

If you did not request a password reset, you can safely ignore this email — your password will remain unchanged.

— Cytova
"""


def render_password_reset(*, first_name: str, reset_url: str, expires_minutes: int) -> Tuple[str, str]:
    """Return (html_body, text_body) for the password-reset email."""
    safe_name = (first_name or 'there').strip() or 'there'
    return (
        _RESET_HTML_TEMPLATE.format(
            first_name=html.escape(safe_name),
            reset_url=reset_url,
            expires_minutes=expires_minutes,
        ),
        _RESET_TEXT_TEMPLATE.format(
            first_name=safe_name,
            reset_url=reset_url,
            expires_minutes=expires_minutes,
        ),
    )


# ---------------------------------------------------------------------------
# Patient result-ready notification
#
# IMPORTANT — confidentiality contract: this template MUST NOT include any
# medical content (no result values, no diagnosis, no exam list, no request
# number, no clinical comments). The email is delivered over plain SMTP and
# may transit unencrypted relays; only the secure access URL belongs in it.
# Add new fields to this template only after auditing them against the same
# rule. The accompanying tests assert that no medical-leaning vocabulary
# appears in the rendered output.
# ---------------------------------------------------------------------------

_RESULT_READY_DEFAULT_TITLE = 'Your lab result is ready'


def _render_html_body_paragraphs(rendered_body: str) -> str:
    """Convert the operator's rendered body (plain text, possibly
    multi-paragraph) into a sequence of HTML ``<tr><td>`` rows that
    slot into the branded shell.

    Splits on blank lines for paragraph breaks, then on single
    newlines for soft line breaks. Each chunk is HTML-escaped — the
    operator's text is treated as plain text, never as raw HTML, so
    a name like ``"O'Brien"`` or a stray ``<`` can't break the
    surrounding markup. The placeholder substitution itself already
    escaped the four allowed variables (the renderer was called
    with ``escape_html=True``); this layer escapes the operator's
    *literal* characters.
    """
    paragraphs = [p for p in rendered_body.split('\n\n') if p.strip()]
    cells = []
    for para in paragraphs:
        # Soft line breaks within a paragraph render as ``<br>``.
        # Each segment is HTML-escaped so operator-typed angle
        # brackets / ampersands stay literal.
        lines = [html.escape(line) for line in para.split('\n')]
        inner = '<br>'.join(lines)
        cells.append(
            '          <tr>\n'
            '            <td style="font-size:14px; line-height:1.6; '
            'color:#475569; padding-bottom:16px;">\n'
            f'              {inner}\n'
            '            </td>\n'
            '          </tr>'
        )
    return '\n'.join(cells)


# HTML shell with an explicit ``{title}`` slot + a ``{body_paragraphs}``
# slot for the operator's rendered text. The CTA button + footer stay
# fixed (spec §5: "result link should still be shown as a CTA button").
_RESULT_READY_HTML_SHELL = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
</head>
<body style="margin:0; padding:0; background:#f8fafc; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; color:#0f172a;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f8fafc; padding:40px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="480" style="max-width:480px; background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:40px;">
          <tr>
            <td align="center" style="padding-bottom:24px;">
              <span style="display:inline-block; font-size:20px; font-weight:600; letter-spacing:-0.02em; color:#2563eb;">Cytova</span>
            </td>
          </tr>
          <tr>
            <td style="font-size:18px; font-weight:600; color:#0f172a; padding-bottom:8px;">
              {title}
            </td>
          </tr>
{body_paragraphs}
          <tr>
            <td align="center" style="padding:8px 0 24px 0;">
              <a href="{secure_link}" style="display:inline-block; padding:12px 28px; background:#2563eb; color:#ffffff; font-size:14px; font-weight:600; text-decoration:none; border-radius:10px;">
                Access result
              </a>
            </td>
          </tr>
          <tr>
            <td style="font-size:13px; line-height:1.6; color:#64748b; padding-bottom:8px;">
              Or copy and paste this link into your browser:
            </td>
          </tr>
          <tr>
            <td style="font-size:12px; line-height:1.5; color:#2563eb; word-break:break-all; padding-bottom:24px;">
              <a href="{secure_link}" style="color:#2563eb; text-decoration:underline;">{secure_link}</a>
            </td>
          </tr>
          <tr>
            <td style="border-top:1px solid #e2e8f0; padding-top:20px; font-size:12px; line-height:1.5; color:#94a3b8;">
              If you did not expect this email, you can safely ignore it.
            </td>
          </tr>
        </table>
        <div style="font-size:11px; color:#94a3b8; padding-top:16px;">{footer}</div>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def render_patient_result_ready(
    *,
    first_name: str,
    secure_link: str,
    lab_name: str = '',
    request_reference: str = '',
    subject_template: str = '',
    body_template: str = '',
) -> Tuple[str, str, str]:
    """Render the patient result-ready email — branded HTML shell +
    plain-text body + subject line.

    Returns
    -------
    ``(subject, html_body, text_body)``. Phase 2 widened the return
    shape from ``(html, text)`` to a 3-tuple so the caller can use
    the rendered subject directly in ``EmailMessage.subject``
    instead of relying on a separate hard-coded constant.

    Templates
    ---------
    Two operator-customisable inputs (Phase 1's LabSettings fields):

      - ``subject_template`` — empty falls back to
        ``"Your lab result is ready"``. The rendered subject also
        doubles as the in-body bold title so customising one knob
        gives the operator a coherent visual.
      - ``body_template`` — empty falls back to today's hard-coded
        body copy verbatim, so tenants that touch nothing
        experience zero behavioural drift on rollout.

    Both go through ``render_safe_notification_template`` with
    ``escape_html=True`` for HTML rendering and ``escape_html=False``
    for plain-text. The four allow-listed variables are populated
    from the call args; nothing else can be substituted no matter
    what the template references.

    Confidentiality contract (re-asserted from module docstring)
    -----------------------------------------------------------
    The renderer's allow-list is the structural guarantee that no
    medical content can reach the email body — even an operator
    pasting ``"value: {{ result_value }}"`` after a save-time
    bypass would see the literal placeholder, not the value, in
    the rendered output. Phase 1's serializer validator prevents
    the bypass on the standard admin path.

    The CTA button is always rendered (spec §5: "result link
    should still be shown as a CTA button"). Operators who include
    ``{{ result_link }}`` in their body get the URL inline AND the
    button below; operators who omit it get just the button.
    """
    from .safe_template import render_safe_notification_template

    safe_name = (first_name or 'there').strip() or 'there'
    safe_lab = (lab_name or '').strip()
    footer = safe_lab or 'Cytova'

    # Build the four-variable context once. Same dict is used for
    # subject + HTML body + text body; the renderer is purely a
    # function of (template, context, escape_html).
    context = {
        'patient_first_name': safe_name,
        'lab_name': safe_lab,
        'result_link': secure_link,
        'request_reference': request_reference or '',
    }

    # Subject. Empty template → canonical default. Subjects are
    # plain text (never HTML), so escape_html=False.
    subject = (
        render_safe_notification_template(
            subject_template, context, escape_html=False,
        ).strip()
        or _RESULT_READY_DEFAULT_TITLE
    )

    # Body — text version. Operator's text comes through verbatim
    # (newlines preserved). Empty template falls back to today's
    # default, byte-for-byte identical to pre-Phase-2.
    if body_template:
        text_body = (
            render_safe_notification_template(
                body_template, context, escape_html=False,
            )
            + f'\n\n— {footer}\n'
        )
    else:
        # The pre-Phase-2 default text body verbatim, so a tenant
        # that never touches the field gets exactly today's email.
        text_body = (
            f'Hi {safe_name},\n\n'
            f'Your lab result is ready. You can access it securely '
            f'using the link below:\n\n'
            f'  {secure_link}\n\n'
            f'For your privacy, the result file may require a '
            f'password to open.\n\n'
            f'If you did not expect this email, you can safely '
            f'ignore it.\n\n'
            f'— {footer}\n'
        )

    # Body — HTML version. Operator's text gets paragraph-broken +
    # HTML-escaped. Empty template falls back to today's default
    # paragraph (single line).
    if body_template:
        # Render with ``escape_html=False`` because the paragraph
        # formatter below already calls ``html.escape`` on every
        # line. Escaping at substitution time too would double-
        # escape: ``"<Ada>"`` would become ``&amp;lt;Ada&amp;gt;``
        # instead of the correct ``&lt;Ada&gt;``. Single escape
        # pass at the paragraph level handles both substituted
        # values AND operator-typed literal characters uniformly.
        rendered_html_body = render_safe_notification_template(
            body_template, context, escape_html=False,
        )
        body_paragraphs = _render_html_body_paragraphs(rendered_html_body)
    else:
        # Reproduce the pre-Phase-2 single body paragraph.
        body_paragraphs = (
            '          <tr>\n'
            '            <td style="font-size:14px; line-height:1.6; '
            'color:#475569; padding-bottom:24px;">\n'
            f'              Hi {html.escape(safe_name)}, your lab '
            f'result is ready. You can access it securely using the '
            f'link below.\n'
            '            </td>\n'
            '          </tr>'
        )

    html_body = _RESULT_READY_HTML_SHELL.format(
        title=html.escape(subject),
        body_paragraphs=body_paragraphs,
        secure_link=secure_link,
        footer=html.escape(footer),
    )
    return (subject, html_body, text_body)


# ---------------------------------------------------------------------------
# Patient Portal — email verification (link, not code)
# ---------------------------------------------------------------------------

_PATIENT_VERIFY_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Verify your Cytova account</title>
</head>
<body style="margin:0; padding:0; background:#f8fafc; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; color:#0f172a;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f8fafc; padding:40px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="480" style="max-width:480px; background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:40px;">
          <tr>
            <td align="center" style="padding-bottom:24px;">
              <span style="display:inline-block; font-size:20px; font-weight:600; letter-spacing:-0.02em; color:#2563eb;">Cytova</span>
            </td>
          </tr>
          <tr>
            <td style="font-size:18px; font-weight:600; color:#0f172a; padding-bottom:8px;">
              Verify your Cytova account
            </td>
          </tr>
          <tr>
            <td style="font-size:14px; line-height:1.6; color:#475569; padding-bottom:24px;">
              Hi {first_name}, welcome to Cytova. Please confirm your email address to activate your patient account.
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:8px 0 24px 0;">
              <a href="{verify_url}" style="display:inline-block; padding:12px 28px; background:#2563eb; color:#ffffff; font-size:14px; font-weight:600; text-decoration:none; border-radius:10px;">
                Verify my email
              </a>
            </td>
          </tr>
          <tr>
            <td style="font-size:13px; line-height:1.6; color:#64748b; padding-bottom:24px;">
              This link is valid for {expires_hours} hours and can be used only once.
            </td>
          </tr>
          <tr>
            <td style="border-top:1px solid #e2e8f0; padding-top:20px; font-size:12px; line-height:1.5; color:#94a3b8;">
              If you did not create a Cytova account, you can safely ignore this email.
            </td>
          </tr>
        </table>
        <div style="font-size:11px; color:#94a3b8; padding-top:16px;">© Cytova</div>
      </td>
    </tr>
  </table>
</body>
</html>
"""


_PATIENT_VERIFY_TEXT_TEMPLATE = """\
Hi {first_name},

Welcome to Cytova. Please confirm your email address by opening the link below:

  {verify_url}

This link is valid for {expires_hours} hours and can be used only once.

If you did not create a Cytova account, you can safely ignore this email.

— Cytova
"""


def render_patient_verification(
    *, first_name: str, verify_url: str, expires_hours: int,
) -> Tuple[str, str]:
    """Return (html_body, text_body) for the patient email-verification email."""
    safe_name = (first_name or 'there').strip() or 'there'
    return (
        _PATIENT_VERIFY_HTML_TEMPLATE.format(
            first_name=html.escape(safe_name),
            verify_url=verify_url,
            expires_hours=expires_hours,
        ),
        _PATIENT_VERIFY_TEXT_TEMPLATE.format(
            first_name=safe_name,
            verify_url=verify_url,
            expires_hours=expires_hours,
        ),
    )


# ---------------------------------------------------------------------------
# Patient Portal — "new shared result" notification
#
# IMPORTANT — confidentiality contract: this template MUST NOT include
# any medical content (no exam names, no values, no result reference,
# no clinical comments, no PDF). The email is a generic "log in to
# Cytova" prompt. The patient sees the actual result only after they
# authenticate to the portal and download via the per-file token
# endpoint.
# ---------------------------------------------------------------------------

_PATIENT_SHARED_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>New lab result available in Cytova</title>
</head>
<body style="margin:0; padding:0; background:#f8fafc; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; color:#0f172a;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f8fafc; padding:40px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="480" style="max-width:480px; background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:40px;">
          <tr>
            <td align="center" style="padding-bottom:24px;">
              <span style="display:inline-block; font-size:20px; font-weight:600; letter-spacing:-0.02em; color:#2563eb;">Cytova</span>
            </td>
          </tr>
          <tr>
            <td style="font-size:18px; font-weight:600; color:#0f172a; padding-bottom:8px;">
              New lab result available
            </td>
          </tr>
          <tr>
            <td style="font-size:14px; line-height:1.6; color:#475569; padding-bottom:24px;">
              A laboratory has shared a result with your Cytova patient space.
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:8px 0 24px 0;">
              <a href="{view_url}" style="display:inline-block; padding:12px 28px; background:#2563eb; color:#ffffff; font-size:14px; font-weight:600; text-decoration:none; border-radius:10px;">
                View my results
              </a>
            </td>
          </tr>
          <tr>
            <td style="font-size:13px; line-height:1.6; color:#64748b; padding-bottom:24px;">
              For your privacy, sign in to your Cytova account to view or download your result.
            </td>
          </tr>
          <tr>
            <td style="border-top:1px solid #e2e8f0; padding-top:20px; font-size:12px; line-height:1.5; color:#94a3b8;">
              If you were not expecting this result, contact the laboratory that performed the analysis.
            </td>
          </tr>
        </table>
        <div style="font-size:11px; color:#94a3b8; padding-top:16px;">© Cytova</div>
      </td>
    </tr>
  </table>
</body>
</html>
"""


_PATIENT_SHARED_TEXT_TEMPLATE = """\
New lab result available

A laboratory has shared a result with your Cytova patient space.

For your privacy, sign in to your Cytova account to view or download your result.

  {view_url}

If you were not expecting this result, contact the laboratory that performed the analysis.

— Cytova
"""


def render_patient_shared_result(*, view_url: str) -> Tuple[str, str]:
    """Return (html_body, text_body) for the "result shared with you"
    patient email. Caller MUST NOT pass any medical content; this
    template renders only the generic prompt + sign-in CTA."""
    return (
        _PATIENT_SHARED_HTML_TEMPLATE.format(view_url=view_url),
        _PATIENT_SHARED_TEXT_TEMPLATE.format(view_url=view_url),
    )


# ---------------------------------------------------------------------------
# Internal-staff workflow templates
#
# Confidentiality contract: these templates ARE allowed to mention
# request reference, exam name, and the rejection comment (which is
# operator-written feedback for the technician — not a result value).
# They MUST NOT include:
#   - any result value / numeric measurement
#   - any patient first name, last name, DOB, document number, or email
#   - any reference range
#   - any clinical interpretation
#
# The accompanying tests grep the rendered output to keep this honest.
# ---------------------------------------------------------------------------

_BIOLOGIST_READY_TEXT_TEMPLATE = """\
Hi {first_name},

A laboratory request is ready for biological validation.

Request: {request_reference}
{exam_summary_block}

Open this request in Cytova to review and validate the results:
{review_url}

— Cytova
You're receiving this because you are a biologist on this laboratory.
"""

_BIOLOGIST_READY_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f8fafc;padding:40px 16px;">
    <tr><td align="center">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="520" style="max-width:520px;background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;padding:32px;">
        <tr><td style="padding-bottom:16px;font-size:13px;color:#64748b;letter-spacing:0.04em;">CYTOVA · INTERNAL</td></tr>
        <tr><td style="padding-bottom:8px;font-size:18px;font-weight:600;color:#0f172a;">Request ready for biological validation</td></tr>
        <tr><td style="padding-bottom:16px;font-size:14px;line-height:1.6;color:#475569;">Hi {first_name}, all required exam results have been submitted on this request and are awaiting your validation.</td></tr>
        <tr><td style="padding-bottom:16px;font-size:14px;line-height:1.6;color:#0f172a;">
          <div style="font-weight:600;">Request: {request_reference}</div>
          {exam_summary_html}
        </td></tr>
        <tr><td align="center" style="padding:8px 0 24px 0;">
          <a href="{review_url}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:10px;font-size:14px;font-weight:600;">Review request in Cytova</a>
        </td></tr>
        <tr><td style="padding-top:16px;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8;line-height:1.6;">You're receiving this because you are a biologist on this laboratory. This notification carries no result values or patient details — open Cytova to see them.</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
"""


def render_biologist_request_ready(
    *,
    first_name: str,
    request_reference: str,
    exam_names: list[str],
    review_url: str,
) -> Tuple[str, str]:
    """Render the "request ready for biological validation" email.

    ``exam_names`` is a small list of exam display names — purely
    metadata, never a result value. Anything more clinical (units,
    reference ranges, computed values) belongs only behind the
    review link.

    Caller must already have filtered the list to a reasonable
    size; we don't truncate here so the audit content is verbatim
    what the recipient sees.
    """
    safe_name = (first_name or 'there').strip() or 'there'
    safe_ref = (request_reference or '').strip() or '(no reference)'

    # Build a concise, escape-safe summary block. We keep it to a
    # short bullet list so the email stays scannable.
    text_summary = '\n'.join(
        f'  - {n}' for n in exam_names if n
    )
    if text_summary:
        text_block = f'Exams:\n{text_summary}'
    else:
        text_block = '(see request in Cytova for the full exam list)'

    html_items = ''.join(
        f'<li style="margin:4px 0;color:#475569;">{html.escape(n)}</li>'
        for n in exam_names if n
    )
    if html_items:
        html_summary = (
            '<ul style="margin:8px 0 0 0;padding-left:20px;font-size:14px;">'
            + html_items
            + '</ul>'
        )
    else:
        html_summary = (
            '<div style="font-size:13px;color:#64748b;margin-top:4px;">'
            'See the request in Cytova for the exam list.</div>'
        )

    return (
        _BIOLOGIST_READY_HTML_TEMPLATE.format(
            first_name=html.escape(safe_name),
            request_reference=html.escape(safe_ref),
            exam_summary_html=html_summary,
            review_url=review_url,
        ),
        _BIOLOGIST_READY_TEXT_TEMPLATE.format(
            first_name=safe_name,
            request_reference=safe_ref,
            exam_summary_block=text_block,
            review_url=review_url,
        ),
    )


_TECH_REJECTED_TEXT_TEMPLATE = """\
Hi {first_name},

A result you submitted has been rejected by a biologist and needs to be
re-entered.

Request: {request_reference}
Exam:    {exam_name}
{notes_block}

Open the request in Cytova to enter a corrected result:
{review_url}

— Cytova
"""

_TECH_REJECTED_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f8fafc;padding:40px 16px;">
    <tr><td align="center">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="520" style="max-width:520px;background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;padding:32px;">
        <tr><td style="padding-bottom:16px;font-size:13px;color:#b45309;letter-spacing:0.04em;">CYTOVA · INTERNAL</td></tr>
        <tr><td style="padding-bottom:8px;font-size:18px;font-weight:600;color:#0f172a;">A result you submitted was rejected</td></tr>
        <tr><td style="padding-bottom:16px;font-size:14px;line-height:1.6;color:#475569;">Hi {first_name}, a biologist asked for this result to be re-entered.</td></tr>
        <tr><td style="padding-bottom:16px;font-size:14px;line-height:1.6;color:#0f172a;">
          <div><span style="color:#64748b;">Request:</span> <strong>{request_reference}</strong></div>
          <div><span style="color:#64748b;">Exam:</span> <strong>{exam_name}</strong></div>
          {notes_html}
        </td></tr>
        <tr><td align="center" style="padding:8px 0 24px 0;">
          <a href="{review_url}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:10px;font-size:14px;font-weight:600;">Open request in Cytova</a>
        </td></tr>
        <tr><td style="padding-top:16px;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8;line-height:1.6;">This notification only mentions the request reference, the exam name, and the rejection note. Open Cytova to see the rest.</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
"""


def render_technician_result_rejected(
    *,
    first_name: str,
    request_reference: str,
    exam_name: str,
    rejection_notes: str,
    review_url: str,
) -> Tuple[str, str]:
    """Render the "your submitted result was rejected" email.

    The rejection note is operator-written feedback (biologist →
    technician). It MAY contain context like "wrong sample tube"
    or "value looks too low — please re-check"; that's the point.
    Callers are responsible for not pasting clinical content
    there, but the template doesn't try to police the note.
    """
    safe_name = (first_name or 'there').strip() or 'there'
    safe_ref = (request_reference or '').strip() or '(no reference)'
    safe_exam = (exam_name or '').strip() or '(unnamed exam)'
    notes = (rejection_notes or '').strip()

    if notes:
        notes_text = f'Note:    {notes}'
        notes_html = (
            '<div style="margin-top:8px;padding:12px;background:#fef3c7;'
            'border-left:3px solid #f59e0b;font-size:13px;color:#78350f;">'
            f'{html.escape(notes)}</div>'
        )
    else:
        notes_text = ''
        notes_html = ''

    return (
        _TECH_REJECTED_HTML_TEMPLATE.format(
            first_name=html.escape(safe_name),
            request_reference=html.escape(safe_ref),
            exam_name=html.escape(safe_exam),
            notes_html=notes_html,
            review_url=review_url,
        ),
        _TECH_REJECTED_TEXT_TEMPLATE.format(
            first_name=safe_name,
            request_reference=safe_ref,
            exam_name=safe_exam,
            notes_block=notes_text,
            review_url=review_url,
        ),
    )
