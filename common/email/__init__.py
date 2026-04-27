"""
Cytova — Email infrastructure.

Provider-agnostic transactional email layer. Domain code (e.g. onboarding
verification) imports `EmailService` / `get_email_service`; the active
provider is resolved from `settings.EMAIL_PROVIDER` (default: console).

Adding a new provider — say SES or Resend — only requires:
  1. A new `EmailProvider` subclass under `common/email/providers/`.
  2. A branch in `EmailService.from_settings` reading provider-specific
     settings (API key, sender, etc.).

Domain code stays untouched.
"""
from .service import EmailService, get_email_service  # noqa: F401
from .providers.base import EmailMessage, EmailProvider, EmailResult  # noqa: F401
