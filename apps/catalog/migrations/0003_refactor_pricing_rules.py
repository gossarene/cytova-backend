# Generated manually — Pricing refactor

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('catalog', '0002_initial'),
        ('partners', '0001_initial'),
    ]

    operations = [
        # --- ExamDefinition: add unit_price ---
        migrations.AddField(
            model_name='examdefinition',
            name='unit_price',
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text='Reference/default catalog price for this exam.',
                max_digits=12,
            ),
        ),

        # --- PricingRule: remove old fields ---
        migrations.RemoveField(
            model_name='pricingrule',
            name='unit_price',
        ),
        migrations.RemoveField(
            model_name='pricingrule',
            name='billed_price',
        ),
        migrations.RemoveField(
            model_name='pricingrule',
            name='effective_from',
        ),
        migrations.RemoveField(
            model_name='pricingrule',
            name='effective_to',
        ),
        migrations.RemoveField(
            model_name='pricingrule',
            name='insurance_code',
        ),
        # Remove old created_at (non-BaseModel version)
        migrations.RemoveField(
            model_name='pricingrule',
            name='created_at',
        ),

        # --- PricingRule: add new fields ---
        migrations.AddField(
            model_name='pricingrule',
            name='partner_organization',
            field=models.ForeignKey(
                blank=True,
                help_text='Set to target a specific partner. NULL = not partner-specific.',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='pricing_rules',
                to='partners.partnerorganization',
            ),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='source_type',
            field=models.CharField(
                blank=True,
                default='',
                help_text='DIRECT_PATIENT or PARTNER_ORGANIZATION. Empty = any source type.',
                max_length=25,
            ),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='pricing_type',
            field=models.CharField(
                choices=[('FIXED_PRICE', 'Fixed Price'), ('PERCENTAGE_DISCOUNT', 'Percentage Discount')],
                default='FIXED_PRICE',
                max_length=25,
            ),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='value',
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text='For FIXED_PRICE: the absolute billed price. For PERCENTAGE_DISCOUNT: the discount percentage (e.g. 10 = 10%% off).',
                max_digits=12,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='priority',
            field=models.IntegerField(
                default=0,
                help_text='Higher value = higher priority within the same specificity level.',
            ),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='is_active',
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='start_date',
            field=models.DateField(
                blank=True,
                help_text='Rule is active from this date (inclusive). NULL = no lower bound.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='end_date',
            field=models.DateField(
                blank=True,
                help_text='Rule is active until this date (inclusive). NULL = no upper bound.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='notes',
            field=models.TextField(blank=True, default=''),
        ),
        # Add BaseModel fields: created_at (new format) and updated_at
        migrations.AddField(
            model_name='pricingrule',
            name='created_at',
            field=models.DateTimeField(db_index=True, default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='pricingrule',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),

        # --- PricingRule: update indexes ---
        migrations.RemoveIndex(
            model_name='pricingrule',
            name='catalog_pri_exam_de_942a8d_idx',
        ),
        migrations.AddIndex(
            model_name='pricingrule',
            index=models.Index(fields=['exam_definition', 'is_active'], name='catalog_pri_exam_de_active_idx'),
        ),
        migrations.AddIndex(
            model_name='pricingrule',
            index=models.Index(fields=['partner_organization', 'is_active'], name='catalog_pri_partner_active_idx'),
        ),

        # --- PricingRule: update ordering ---
        migrations.AlterModelOptions(
            name='pricingrule',
            options={
                'ordering': ['-priority', '-created_at'],
                'verbose_name': 'Pricing Rule',
                'verbose_name_plural': 'Pricing Rules',
            },
        ),
    ]
