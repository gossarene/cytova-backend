from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0002_alter_auditlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(
                choices=[
                    ('CREATE', 'Create'),
                    ('UPDATE', 'Update'),
                    ('DELETE', 'Delete'),
                    ('LOGIN', 'Login'),
                    ('LOGIN_FAILED', 'Login Failed'),
                    ('LOGOUT', 'Logout'),
                    ('VIEW', 'View'),
                    ('VALIDATE', 'Validate'),
                    ('PUBLISH', 'Publish'),
                    ('CONFIRM', 'Confirm'),
                    ('CANCEL', 'Cancel'),
                    ('DEACTIVATE', 'Deactivate'),
                    ('REACTIVATE', 'Reactivate'),
                    ('TOKEN_REVOKED', 'Token Revoked'),
                    ('PASSWORD_RESET', 'Password Reset'),
                    ('ROLE_ASSIGN', 'Role Assign'),
                    ('PERMISSION_OVERRIDE', 'Permission Override'),
                ],
                db_index=True,
                max_length=20,
            ),
        ),
    ]
