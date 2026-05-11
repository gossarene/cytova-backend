"""
Cytova Core — team management service.

Encapsulates the four state-changing operations on
``PlatformAdminUser`` so the view layer stays thin and the
last-super-admin invariant has one canonical owner.

Invariants enforced here
------------------------
1. There is ALWAYS at least one active ``SUPER_ADMIN`` after any
   admin-management operation. Otherwise the platform would
   become un-administerable from the moment the last super admin
   left the role.
2. An admin cannot deactivate themselves while they are the last
   active super admin. They can step down by first creating
   another super admin.
3. Roles cannot be downgraded below SUPER_ADMIN on the last
   active super admin — same reasoning.

The locks taken here are ``select_for_update`` on the candidate
super-admin rows. That serialises concurrent role / activation
changes against each other so a race between two SUPER_ADMINs each
demoting the other cannot strand the platform.

Auditing
--------
This module does NOT write audit rows. The view layer is the
audit source of truth (so a service-side failure surfaces as a
4xx without a misleading "happened" row in the log).
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import PlatformAdminRole, PlatformAdminUser


SUPER_ADMIN = PlatformAdminRole.SUPER_ADMIN.value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_super_admin_count(exclude_pk=None) -> int:
    """Count active SUPER_ADMINs, optionally excluding a candidate.

    Used to verify that an operation will not leave the platform
    with zero active super admins. ``exclude_pk`` is the row about
    to be mutated — the count of *others* is the load-bearing
    number.
    """
    qs = PlatformAdminUser.objects.filter(
        role=SUPER_ADMIN, is_active=True,
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.count()


def _generate_temp_password(length: int = 24) -> str:
    """Cryptographically-strong temporary password.

    Used for V1 ``create_admin``: the new admin gets an inactive-
    until-password-set workflow once email plumbing is in place;
    until then the temporary password is returned ONCE to the
    inviting super admin (in the API response). It is never
    persisted, never logged, never written to audit metadata.

    Uses ``secrets`` (not ``random``) for cryptographic strength.
    """
    alphabet = string.ascii_letters + string.digits + '!@#$%^&*-_=+'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DuplicateEmailError(ValidationError):
    def __init__(self):
        super().__init__({
            'detail': 'A platform admin already exists with this email.',
            'code': 'DUPLICATE_EMAIL',
        })


class LastSuperAdminError(ValidationError):
    """The operation would leave zero active SUPER_ADMINs."""
    def __init__(self, message: str):
        super().__init__({
            'detail': message,
            'code': 'LAST_SUPER_ADMIN',
        })


class SelfDeactivationError(ValidationError):
    """An admin cannot deactivate themselves while irreplaceable."""
    def __init__(self):
        super().__init__({
            'detail':
                'You are the last active super admin. Promote another '
                'super admin before deactivating yourself.',
            'code': 'SELF_DEACTIVATION_BLOCKED',
        })


# ---------------------------------------------------------------------------
# Result carrier
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreatedAdmin:
    """Carrier for ``create_admin``. ``temporary_password`` is
    returned to the inviting super admin ONCE (in the API
    response) so they can hand it to the new admin out of band.
    Subsequent reads of the same admin row will not surface it
    again — it is not stored anywhere except the user's hash."""
    user: PlatformAdminUser
    temporary_password: str


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

@transaction.atomic
def create_admin(
    *,
    email: str,
    first_name: str = '',
    last_name: str = '',
    role: str,
) -> CreatedAdmin:
    """Create a new platform admin row.

    The temporary password is generated server-side so the caller
    cannot accidentally seed a weak credential. It is returned in
    ``CreatedAdmin.temporary_password`` and the caller is
    responsible for showing it once + telling the new admin to
    change it on first sign-in.
    """
    email = email.strip().lower()
    if PlatformAdminUser.objects.filter(email__iexact=email).exists():
        raise DuplicateEmailError()

    if role not in {r.value for r in PlatformAdminRole}:
        raise ValidationError({
            'role': f'Unknown role {role!r}.', 'code': 'INVALID_ROLE',
        })

    password = _generate_temp_password()
    user = PlatformAdminUser.objects.create_user(
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
        role=role,
        is_active=True,
    )
    return CreatedAdmin(user=user, temporary_password=password)


@transaction.atomic
def deactivate_admin(
    *, target: PlatformAdminUser, actor: PlatformAdminUser,
) -> PlatformAdminUser:
    """Deactivate ``target``. Refuses if the operation would leave
    the platform without an active SUPER_ADMIN, or if the actor is
    deactivating themselves while irreplaceable.

    The ``select_for_update`` lock on candidate super admins
    serialises concurrent calls so two parallel deactivations
    cannot both think they leave a survivor when neither does.
    """
    # Take the lock BEFORE the count so a concurrent transaction
    # can't slip in a deactivation between our count and our save.
    list(
        PlatformAdminUser.objects
        .select_for_update()
        .filter(role=SUPER_ADMIN, is_active=True)
    )

    if target.role == SUPER_ADMIN and target.is_active:
        if _active_super_admin_count(exclude_pk=target.pk) == 0:
            if actor.pk == target.pk:
                raise SelfDeactivationError()
            raise LastSuperAdminError(
                'Cannot deactivate the last active super admin.',
            )

    if not target.is_active:
        return target

    target.is_active = False
    target.save(update_fields=['is_active', 'updated_at'])
    return target


@transaction.atomic
def reactivate_admin(*, target: PlatformAdminUser) -> PlatformAdminUser:
    """Reactivate ``target``. Idempotent on an already-active row."""
    if target.is_active:
        return target
    target.is_active = True
    target.save(update_fields=['is_active', 'updated_at'])
    return target


@transaction.atomic
def change_role(
    *, target: PlatformAdminUser, new_role: str, actor: PlatformAdminUser,
) -> PlatformAdminUser:
    """Switch ``target.role`` to ``new_role``.

    The last-super-admin guard fires when:
      - target is currently SUPER_ADMIN, is_active=True,
      - new_role is not SUPER_ADMIN, AND
      - no other active SUPER_ADMIN exists.

    The self-demotion case shares the same code path: an actor
    cannot demote themselves out of SUPER_ADMIN while irreplaceable.
    """
    if new_role not in {r.value for r in PlatformAdminRole}:
        raise ValidationError({
            'role': f'Unknown role {new_role!r}.', 'code': 'INVALID_ROLE',
        })

    list(
        PlatformAdminUser.objects
        .select_for_update()
        .filter(role=SUPER_ADMIN, is_active=True)
    )

    if (
        target.role == SUPER_ADMIN
        and target.is_active
        and new_role != SUPER_ADMIN
        and _active_super_admin_count(exclude_pk=target.pk) == 0
    ):
        if actor.pk == target.pk:
            raise LastSuperAdminError(
                'You are the last active super admin. Promote another '
                'super admin before demoting yourself.',
            )
        raise LastSuperAdminError(
            'Cannot demote the last active super admin.',
        )

    if target.role == new_role:
        return target

    target.role = new_role
    target.save(update_fields=['role', 'updated_at'])
    return target


__all__ = [
    'create_admin', 'deactivate_admin', 'reactivate_admin', 'change_role',
    'CreatedAdmin',
    'DuplicateEmailError', 'LastSuperAdminError', 'SelfDeactivationError',
]
