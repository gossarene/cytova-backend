"""
Cytova Core — secure bootstrap for the first PlatformAdminUser.

There is NO public signup for ``core.cytova.io``. The first
super-admin is created through this management command, on a host
with shell access to the backend. Subsequent admins are created
through the ``/api/v1/platform-admin/team/`` API, by an existing
SUPER_ADMIN.

Usage
-----
Bootstrap the first super-admin (interactive password prompt)::

    python manage.py create_platform_admin --email founder@cytova.io

With a password supplied (CI / scripted setups — typically read
from a secret manager rather than passed on the command line)::

    python manage.py create_platform_admin \
        --email founder@cytova.io \
        --first-name Ada --last-name Lovelace \
        --password "$BOOTSTRAP_PW"

Non-SUPER_ADMIN bootstrap (rare — usually use the API instead)::

    python manage.py create_platform_admin \
        --email auditor@cytova.io \
        --role READ_ONLY_AUDITOR \
        --allow-non-super-admin

Safety contract
---------------
- Passwords are hashed via Django's configured KDF before the user
  row is committed. The plaintext never lands on disk.
- The plaintext is never echoed back to stdout — only a generic
  success line and the new user's id / email / role.
- Duplicate emails are refused (case-insensitive) so an accidental
  re-run does not silently take over an existing account.
- By default the role is pinned to SUPER_ADMIN. Pass
  ``--allow-non-super-admin`` to opt into another role from this
  CLI; the API is the preferred path for non-bootstrap creations.
"""
from __future__ import annotations

import getpass
import sys

from django.contrib.auth.password_validation import (
    ValidationError as PasswordValidationError, validate_password,
)
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.platform_admin.models import PlatformAdminRole, PlatformAdminUser


class Command(BaseCommand):
    help = (
        'Create a platform-admin user (bootstrap). '
        'Use the /platform-admin/team/ API for subsequent admins.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--email', required=True,
            help='Admin email (used as the login identifier).',
        )
        parser.add_argument(
            '--first-name', default='',
            help='Optional given name. Defaults to empty.',
        )
        parser.add_argument(
            '--last-name', default='',
            help='Optional surname. Defaults to empty.',
        )
        parser.add_argument(
            '--role',
            choices=[r.value for r in PlatformAdminRole],
            default=PlatformAdminRole.SUPER_ADMIN.value,
            help=(
                'Role for the new admin. Defaults to SUPER_ADMIN. '
                'Non-SUPER_ADMIN roles require --allow-non-super-admin.'
            ),
        )
        parser.add_argument(
            '--password', default=None,
            help=(
                'Plaintext password. If omitted, you are prompted '
                'interactively (preferred — keeps the password out '
                'of shell history and process listings).'
            ),
        )
        parser.add_argument(
            '--allow-non-super-admin', action='store_true',
            help=(
                'Required when --role is not SUPER_ADMIN. Forces the '
                'caller to opt in to non-bootstrap creations from the '
                'CLI; the API is the recommended path for those.'
            ),
        )
        parser.add_argument(
            '--no-input', action='store_true',
            help=(
                'Refuse to prompt for a password. Useful in CI: combine '
                'with --password to keep the command non-interactive.'
            ),
        )

    def handle(self, *args, **options):
        email: str = options['email'].strip().lower()
        first_name: str = options['first_name']
        last_name: str = options['last_name']
        role: str = options['role']
        password: str | None = options['password']
        allow_non_super = options['allow_non_super_admin']
        no_input = options['no_input']

        # ---- Role gate ----
        # Default is SUPER_ADMIN; any deviation requires explicit
        # opt-in. This keeps the bootstrap path narrow — accidental
        # CLI use to create an auditor (or to demote the bootstrap)
        # fails loudly.
        if role != PlatformAdminRole.SUPER_ADMIN.value and not allow_non_super:
            raise CommandError(
                f'--role={role} requires --allow-non-super-admin. '
                'Prefer creating non-super admins via the team API.'
            )

        # ---- Duplicate-email refusal ----
        # Case-insensitive lookup mirrors the model's
        # ``USERNAME_FIELD = email`` + ``normalize_email`` behaviour.
        if PlatformAdminUser.objects.filter(email__iexact=email).exists():
            raise CommandError(
                f'A platform admin already exists with email {email!r}. '
                'Use the team API to manage existing admins.'
            )

        # ---- Password ----
        if password is None:
            if no_input:
                raise CommandError(
                    '--password is required when --no-input is set.'
                )
            password = self._prompt_password()

        # Reject weak passwords up front so a bootstrap can't seed a
        # weakly-hashed credential into prod. We validate without a
        # user instance because validators that compare to user
        # attributes only run when a user is supplied.
        try:
            validate_password(password)
        except PasswordValidationError as exc:
            raise CommandError(
                'Password rejected by validators: '
                + '; '.join(exc.messages)
            )

        # ---- Persist ----
        with transaction.atomic():
            user = PlatformAdminUser.objects.create_user(
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                role=role,
                is_active=True,
            )

        # ---- Output ----
        # Pin the trio (id / email / role) so an operator can verify
        # the bootstrap landed where they expected. The password is
        # NEVER echoed.
        self.stdout.write(self.style.SUCCESS(
            f'Platform admin created: {user.email} '
            f'(role={user.role}, id={user.id})',
        ))

    # ------------------------------------------------------------------
    # Interactive password entry
    # ------------------------------------------------------------------

    def _prompt_password(self) -> str:
        """Prompt twice + compare, like ``createsuperuser``.

        Reads from stdin through ``getpass`` so the password is not
        echoed back to the terminal and does not land in shell
        history. The second prompt is a confirmation so a typo on
        bootstrap doesn't lock a brand-new platform out of its own
        admin surface.
        """
        if not sys.stdin.isatty():
            raise CommandError(
                'Password prompt requires an interactive terminal. '
                'Pass --password or run on a TTY.'
            )
        for _ in range(3):
            pw1 = getpass.getpass('Password: ')
            if not pw1:
                self.stderr.write('Password cannot be empty.')
                continue
            pw2 = getpass.getpass('Password (again): ')
            if pw1 != pw2:
                self.stderr.write("Passwords don't match. Try again.")
                continue
            return pw1
        raise CommandError(
            'Failed to read a matching password after 3 attempts.'
        )
