"""
Cytova — Result PDF branding resolver.

The result PDF normally carries the laboratory's identity (name, logo,
address, footer). When a request is sourced from a partner organization
and that partner has opted into custom branding, the partner's identity
is used in place of the lab's — but only the branding fields, not
display preferences or PDF protection. ``LabSettings`` keeps full
control over toggles such as ``show_logo``, ``show_lab_address``,
``logo_position``, ``logo_max_width_mm``, and the password protection
config; the partner only overrides the *content* shown.

Fallback rules
--------------
- Direct (no partner) requests        → lab branding.
- Partner with branding disabled      → lab branding.
- Partner with branding enabled but
  an individual field empty           → fallback to the lab field.
- Partner with no logo uploaded       → fallback to the lab logo.

This is deliberately field-by-field: a partner can ship a header name +
contact info while still relying on the lab's logo, and the report
still renders coherently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReportBranding:
    """
    Resolved branding for a single result PDF render.

    Fields mirror the subset of ``LabSettings`` the renderer reads when
    drawing the header/footer. ``logo_file_key`` is the storage key
    (relative path) the renderer feeds to ``default_storage.open()`` —
    same contract as ``LabSettings.logo_file_key``, so callers can swap
    the source without touching the renderer's logo code.

    ``source`` is informational ("LAB" or "PARTNER") — used by tests and
    can be exposed in audit logs / debug traces if needed later.
    """
    name: str
    subtitle: str
    address: str
    phone: str
    email: str
    website: str
    logo_file_key: str  # may be empty → renderer skips the logo
    legal_footer: str
    source: str  # 'LAB' | 'PARTNER'


def _logo_storage_key(field) -> str:
    """Extract the storage key from a Django ``ImageFieldFile`` (or any
    file-field-like). Returns ``''`` when nothing is attached, matching
    the empty-string convention ``LabSettings.logo_file_key`` uses."""
    if not field:
        return ''
    name = getattr(field, 'name', '') or ''
    return name


def resolve_result_report_branding(analysis_request) -> ReportBranding:
    """
    Compute the effective branding for a result PDF.

    Parameters
    ----------
    analysis_request : AnalysisRequest
        Must expose ``partner_organization`` (FK, may be None) — true of
        ``apps.requests.models.AnalysisRequest``.

    Returns
    -------
    ReportBranding
        Always returns a complete branding object — never raises, never
        returns ``None``. Missing partner fields fall back to the lab's
        equivalent, so the renderer never has to deal with partial data.
    """
    # Imported lazily to avoid a hard module-load coupling between
    # the requests app and the lab_settings app at import time.
    from apps.lab_settings.models import LabSettings

    settings = LabSettings.get_solo()

    lab_branding = ReportBranding(
        name=settings.lab_name or '',
        subtitle=settings.lab_subtitle or '',
        address=settings.address or '',
        phone=settings.phone or '',
        email=settings.email or '',
        website=settings.website or '',
        logo_file_key=settings.logo_file_key or '',
        legal_footer=settings.legal_footer or '',
        source='LAB',
    )

    partner = _maybe_partner(analysis_request)
    if partner is None or not partner.custom_report_branding_enabled:
        return lab_branding

    return ReportBranding(
        name=partner.report_header_name or lab_branding.name,
        subtitle=partner.report_header_subtitle or lab_branding.subtitle,
        address=partner.report_header_address or lab_branding.address,
        phone=partner.report_header_phone or lab_branding.phone,
        email=partner.report_header_email or lab_branding.email,
        # Partners don't have a website field — keep the lab's.
        website=lab_branding.website,
        logo_file_key=(
            _logo_storage_key(partner.report_header_logo)
            or lab_branding.logo_file_key
        ),
        legal_footer=partner.report_footer_text or lab_branding.legal_footer,
        source='PARTNER',
    )


def _maybe_partner(analysis_request) -> Optional[object]:
    """Return the partner FK if present, otherwise ``None``. Wrapped so
    a missing ``partner_organization`` attribute (e.g. in a unit test
    using a stub object) doesn't blow up the resolver."""
    return getattr(analysis_request, 'partner_organization', None)
