# Generated manually — Add role field to PlatformAdmin

from django.db import migrations, models


def set_existing_admins_as_owners(apps, schema_editor):
    """Existing platform admins become platform owners."""
    PlatformAdmin = apps.get_model('tenants', 'PlatformAdmin')
    PlatformAdmin.objects.update(role='PLATFORM_OWNER')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0004_remove_subscriptionplan_trial_days_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='platformadmin',
            name='role',
            field=models.CharField(
                choices=[
                    ('PLATFORM_OWNER', 'Platform Owner'),
                    ('PLATFORM_STAFF', 'Platform Staff'),
                ],
                default='PLATFORM_STAFF',
                max_length=20,
            ),
        ),

        # Existing admins should be owners (they had full access before)
        migrations.RunPython(set_existing_admins_as_owners, noop),
    ]
