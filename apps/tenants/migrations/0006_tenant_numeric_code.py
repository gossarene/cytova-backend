"""
Add per-tenant 4-digit numeric code + the public-schema counter row that
allocates codes. Existing tenants are backfilled sequentially starting at
0001 ordered by created_at, so historical ordering is preserved.
"""
from django.db import migrations, models


TENANT_CODE_WIDTH = 4


def backfill_numeric_codes(apps, schema_editor):
    Tenant = apps.get_model('tenants', 'Tenant')
    TenantCodeCounter = apps.get_model('tenants', 'TenantCodeCounter')

    tenants = list(Tenant.objects.order_by('created_at', 'id'))
    for i, tenant in enumerate(tenants, start=1):
        tenant.numeric_code = f'{i:0{TENANT_CODE_WIDTH}d}'
        tenant.save(update_fields=['numeric_code'])

    counter, _ = TenantCodeCounter.objects.get_or_create(pk=1)
    counter.last_value = len(tenants)
    counter.save(update_fields=['last_value'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0005_platformadmin_role'),
    ]

    operations = [
        migrations.CreateModel(
            name='TenantCodeCounter',
            fields=[
                ('id', models.PositiveSmallIntegerField(primary_key=True, serialize=False, default=1)),
                ('last_value', models.PositiveIntegerField(default=0)),
            ],
            options={
                'verbose_name': 'Tenant Code Counter',
                'verbose_name_plural': 'Tenant Code Counter',
            },
        ),
        # Add as nullable first so backfill can populate existing rows.
        migrations.AddField(
            model_name='tenant',
            name='numeric_code',
            field=models.CharField(
                max_length=4, null=True, editable=False,
                help_text='Stable 4-digit zero-padded tenant identifier used as the '
                          'prefix of generated label codes. Immutable once assigned.',
            ),
        ),
        migrations.RunPython(backfill_numeric_codes, noop),
        migrations.AlterField(
            model_name='tenant',
            name='numeric_code',
            field=models.CharField(
                max_length=4, unique=True, editable=False,
                help_text='Stable 4-digit zero-padded tenant identifier used as the '
                          'prefix of generated label codes. Immutable once assigned.',
            ),
        ),
    ]
