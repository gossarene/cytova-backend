"""
Cytova — Lab onboarding setup-progress composer.

A small builder that returns the LAB_ADMIN's setup checklist for the
dashboard. Each task carries a completed flag derived from real database
state (LabSettings, ExamDefinition, PartnerOrganization, StaffUser) —
nothing is faked.

Returns ``None`` for users that aren't LAB_ADMIN: only owners get the
onboarding nudge on their dashboard. Other roles see the regular cockpit.

Tenant isolation is implicit: every query runs in the active tenant
schema set by ``CytovaTenantMiddleware``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# Task keys are stable identifiers — the frontend uses them as React
# keys and may key per-task copy off them. Don't rename without a
# matching frontend change.
TASK_LAB_PROFILE         = 'lab_profile'
TASK_LAB_LOGO            = 'lab_logo'
TASK_PDF_SETTINGS        = 'pdf_settings'
TASK_CATALOG_EXAMS       = 'catalog_exams'
TASK_PARTNERS            = 'partners'
TASK_TEAM_USERS          = 'team_users'
TASK_PATIENT_NOTIFICATIONS = 'patient_notifications'

# Short, action-flavoured CTA labels used by the onboarding banner.
# Distinct from the task's verbose ``label`` so the banner button can be
# imperative ("Add your first exam") instead of descriptive
# ("Add catalog exams").
_NEXT_STEP_LABELS = {
    TASK_LAB_PROFILE:           'Complete profile',
    TASK_LAB_LOGO:              'Add logo',
    TASK_PDF_SETTINGS:          'Configure PDF',
    TASK_CATALOG_EXAMS:         'Add your first exam',
    TASK_PARTNERS:              'Add partners',
    TASK_TEAM_USERS:            'Invite team',
    TASK_PATIENT_NOTIFICATIONS: 'Enable notifications',
}


def _profile_complete(lab) -> bool:
    """Lab profile is "complete enough to operate" once name + address
    are filled and at least one contact channel exists."""
    return (
        bool(lab.lab_name.strip())
        and bool(lab.address.strip())
        and (bool(lab.phone.strip()) or bool(lab.email.strip()))
    )


def _logo_present(lab) -> bool:
    return bool(lab.logo_file_key) or bool(lab.logo_url)


def _pdf_configured(lab) -> bool:
    """The PDF settings task counts as done once the lab has set EITHER
    a legal footer (visible on every report) OR turned on PDF password
    protection — both signal that the admin has visited the PDF section
    and made an explicit choice."""
    return bool(lab.legal_footer.strip()) or bool(lab.result_pdf_password_enabled)


def build_setup_progress(user) -> Optional[Dict[str, Any]]:
    """Compose the lab-admin setup checklist, or ``None`` for other roles.

    The percentage is computed off REQUIRED tasks only — recommended
    tasks (e.g. partner onboarding) contribute to the visible counters
    but cannot block reaching 100%. That matches the spec's "optional
    partner task does not block 100% mandatory completion".
    """
    role = getattr(user, 'role', None) or ''
    if role != 'LAB_ADMIN':
        return None

    from apps.lab_settings.models import LabSettings
    from apps.catalog.models import ExamDefinition
    from apps.partners.models import PartnerOrganization
    from apps.users.models import StaffUser

    lab = LabSettings.get_solo()

    has_active_exam = ExamDefinition.objects.filter(is_active=True).exists()
    has_partner = PartnerOrganization.objects.filter(is_active=True).exists()
    has_teammate = (
        StaffUser.objects
        .filter(is_active=True)
        .exclude(pk=user.pk)
        .exists()
    )

    tasks: List[Dict[str, Any]] = [
        {
            'key': TASK_LAB_PROFILE,
            'label': 'Complete laboratory profile',
            'description': 'Add name, address and a contact channel so reports identify your lab.',
            'completed': _profile_complete(lab),
            'required': True,
            'href': '/settings/laboratory',
        },
        {
            'key': TASK_LAB_LOGO,
            'label': 'Add laboratory logo',
            'description': 'Upload your logo — it appears on every PDF you deliver.',
            'completed': _logo_present(lab),
            'required': True,
            'href': '/settings/laboratory',
        },
        {
            'key': TASK_PDF_SETTINGS,
            'label': 'Configure result PDF settings',
            'description': 'Set your legal footer or enable PDF password protection.',
            'completed': _pdf_configured(lab),
            'required': True,
            'href': '/settings/laboratory',
        },
        {
            'key': TASK_CATALOG_EXAMS,
            'label': 'Add catalog exams',
            'description': 'Define at least one active exam so requests can be created.',
            'completed': has_active_exam,
            'required': True,
            'href': '/catalog',
        },
        {
            'key': TASK_PARTNERS,
            'label': 'Add partners',
            'description': 'Optional — register partner clinics that send you requests.',
            'completed': has_partner,
            'required': False,
            'href': '/partners',
        },
        {
            'key': TASK_TEAM_USERS,
            'label': 'Add team users',
            'description': 'Invite at least one technician or biologist to share the workload.',
            'completed': has_teammate,
            'required': True,
            'href': '/users',
        },
        {
            'key': TASK_PATIENT_NOTIFICATIONS,
            'label': 'Enable patient notifications',
            'description': 'Turn on email so patients get a secure link to their results.',
            'completed': bool(lab.notification_enable_email),
            'required': True,
            'href': '/settings/laboratory',
        },
    ]

    required = [t for t in tasks if t['required']]
    done_required = sum(1 for t in required if t['completed'])
    percentage = (
        round(done_required / len(required) * 100) if required else 100
    )

    # Next-step picker: first incomplete REQUIRED task drives the banner CTA.
    # If everything required is done, the first incomplete recommended task
    # takes over so the user still gets a single concrete next action. When
    # the whole list is green, ``next_step`` is None — the frontend uses
    # that as the trigger to show the go-live state.
    next_step = None
    next_task = next(
        (t for t in required if not t['completed']),
        None,
    ) or next(
        (t for t in tasks if not t['completed']),
        None,
    )
    if next_task is not None:
        next_step = {
            'key': next_task['key'],
            'label': _NEXT_STEP_LABELS.get(next_task['key'], 'Continue setup'),
            'url': next_task['href'],
        }

    return {
        'percentage': percentage,
        'completed_count': sum(1 for t in tasks if t['completed']),
        'total_count': len(tasks),
        'tasks': tasks,
        'next_step': next_step,
    }
