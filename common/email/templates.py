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

_RESULT_READY_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your lab result is ready</title>
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
              Your lab result is ready
            </td>
          </tr>
          <tr>
            <td style="font-size:14px; line-height:1.6; color:#475569; padding-bottom:24px;">
              Hi {first_name}, your lab result is ready. You can access it securely using the link below.
            </td>
          </tr>
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
            <td style="font-size:13px; line-height:1.6; color:#64748b; padding-bottom:32px;">
              For your privacy, the result file may require a password to open.
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


_RESULT_READY_TEXT_TEMPLATE = """\
Hi {first_name},

Your lab result is ready. You can access it securely using the link below:

  {secure_link}

For your privacy, the result file may require a password to open.

If you did not expect this email, you can safely ignore it.

— {footer}
"""


def render_patient_result_ready(
    *,
    first_name: str,
    secure_link: str,
    lab_name: str = '',
) -> Tuple[str, str]:
    """Return (html_body, text_body) for the patient result-ready email.

    The body never contains medical data — only a generic notice and the
    secure access URL. ``lab_name`` is optional and only used as a friendly
    sign-off; pass an empty string to fall back to "Cytova".
    """
    safe_name = (first_name or 'there').strip() or 'there'
    safe_lab = (lab_name or '').strip()
    footer = safe_lab or 'Cytova'
    return (
        _RESULT_READY_HTML_TEMPLATE.format(
            first_name=html.escape(safe_name),
            secure_link=secure_link,
            footer=html.escape(footer),
        ),
        _RESULT_READY_TEXT_TEMPLATE.format(
            first_name=safe_name,
            secure_link=secure_link,
            footer=footer,
        ),
    )
