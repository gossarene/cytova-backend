"""
Cytova -- Validate Permission Registry

Validates that the permission registry and role-permission mapping are
consistent. Useful in CI pipelines and after adding new permissions.

Usage:
    python manage.py validate_permissions
    python manage.py validate_permissions --format json
"""
import json

from django.core.management.base import BaseCommand

from apps.users.models import Role
from common.permissions_registry import PermissionRegistry
from common.role_permissions import ROLE_PERMISSIONS, get_role_permissions


class Command(BaseCommand):
    help = 'Validate permission registry and display role-permission mapping.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--format', choices=['text', 'json'], default='text',
            help='Output format (default: text)',
        )

    def handle(self, *args, **options):
        all_codes = PermissionRegistry.codes()
        errors = []

        # Validate: every code uses module.action format
        for code in all_codes:
            if '.' not in code:
                errors.append(f'Permission code missing module.action format: {code}')

        # Validate: every role in ROLE_PERMISSIONS is a valid Role choice
        valid_roles = {r.value for r in Role}
        for role_code in ROLE_PERMISSIONS:
            if role_code not in valid_roles:
                errors.append(f'ROLE_PERMISSIONS references unknown role: {role_code}')

        # Validate: every permission referenced by a role exists in the registry
        for role_code, perms in ROLE_PERMISSIONS.items():
            unknown = perms - all_codes
            for code in sorted(unknown):
                errors.append(f'Role {role_code} references unknown permission: {code}')

        # Validate: every Role choice has an entry in ROLE_PERMISSIONS
        for role_value in valid_roles:
            if role_value not in ROLE_PERMISSIONS:
                errors.append(f'Role {role_value} has no entry in ROLE_PERMISSIONS')

        # Validate: LAB_ADMIN has all permissions
        lab_admin_perms = get_role_permissions('LAB_ADMIN')
        missing_from_admin = all_codes - lab_admin_perms
        for code in sorted(missing_from_admin):
            errors.append(f'LAB_ADMIN is missing permission: {code}')

        if errors:
            for err in errors:
                self.stderr.write(self.style.ERROR(f'  ERROR: {err}'))
            self.stderr.write(self.style.ERROR(f'\n{len(errors)} error(s) found.'))
            raise SystemExit(1)

        if options['format'] == 'json':
            output = {
                'permissions': {
                    code: PermissionRegistry.get(code).description
                    for code in sorted(all_codes)
                },
                'roles': {
                    role: sorted(get_role_permissions(role))
                    for role in sorted(ROLE_PERMISSIONS.keys())
                },
            }
            self.stdout.write(json.dumps(output, indent=2))
        else:
            modules = PermissionRegistry.by_module()
            self.stdout.write(self.style.SUCCESS(
                f'\n  {len(all_codes)} permissions across '
                f'{len(modules)} modules.\n'
            ))
            for module, perms in sorted(modules.items()):
                codes = ', '.join(p.code.split('.')[1] for p in sorted(perms, key=lambda x: x.code))
                self.stdout.write(f'    {module}: {codes}')

            self.stdout.write('')
            for role_code, role_label in Role.choices:
                perms = get_role_permissions(role_code)
                self.stdout.write(f'    {role_label} ({role_code}): {len(perms)} permissions')

        self.stdout.write(self.style.SUCCESS('\n  Validation passed.\n'))
