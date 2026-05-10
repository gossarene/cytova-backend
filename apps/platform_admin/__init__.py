"""
Cytova — Platform Admin app.

Lives in the **public** schema (registered in ``SHARED_APPS``). Houses
the back-office identity used to operate the Cytova platform itself
— distinct from per-tenant lab staff (``apps.users.StaffUser``) and
from global patient identities (``apps.patient_portal.PatientAccount``).

Why a dedicated app
-------------------
Platform admin auth is intentionally separated from:

  - ``apps.tenants`` — owns the Tenant / Subscription / Onboarding
    lifecycle. Mixing administrator credentials into the same app
    that owns the data those administrators operate on blurs the
    layering. ``apps.tenants`` already carries an earlier, narrower
    ``PlatformAdmin`` scaffold (2 roles, no /me, no login audit) used
    by the existing ``/api/v1/platform/`` tenant-CRUD surface. That
    legacy surface is left untouched here so existing flows keep
    working; the canonical admin foundation is this app.
  - ``apps.users`` — per-tenant staff. Tenant tables never get a
    platform-admin row, so the model MUST stay shared-only.
  - ``apps.patient_portal`` — global patient identity. Different
    audience, different lifecycle, different audit boundary.

Token isolation
---------------
The access token sets ``user_type='PLATFORM_ADMIN'`` and the
``PlatformAdminJWTAuthentication`` class refuses any other ``user_type``
value. Combined with the URL routing (these endpoints live only on
the public-schema ``urls_public``), platform-admin tokens cannot
reach lab tenant endpoints, and lab/patient tokens cannot reach
platform-admin endpoints — defence in depth.
"""

default_app_config = 'apps.platform_admin.apps.PlatformAdminConfig'
