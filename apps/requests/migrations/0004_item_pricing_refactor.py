# Generated manually — Add price_source to AnalysisRequestItem, make prices non-nullable

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('analysis_requests', '0003_analysisrequest_billing_mode_and_more'),
        ('catalog', '0003_refactor_pricing_rules'),
    ]

    operations = [
        # Add price_source field
        migrations.AddField(
            model_name='analysisrequestitem',
            name='price_source',
            field=models.CharField(
                choices=[
                    ('DEFAULT_PRICE', 'Default Price'),
                    ('PRICING_RULE', 'Pricing Rule'),
                    ('MANUAL_OVERRIDE', 'Manual Override'),
                ],
                default='DEFAULT_PRICE',
                help_text='How the billed price was determined.',
                max_length=20,
            ),
        ),

        # Make unit_price non-nullable with default 0
        migrations.AlterField(
            model_name='analysisrequestitem',
            name='unit_price',
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text='Reference price snapshotted from ExamDefinition.unit_price at item creation.',
                max_digits=12,
            ),
        ),

        # Make billed_price non-nullable with default 0
        migrations.AlterField(
            model_name='analysisrequestitem',
            name='billed_price',
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text='Actual price charged. May differ from unit_price due to rule or manual override.',
                max_digits=12,
            ),
        ),

        # Update pricing_rule FK help_text
        migrations.AlterField(
            model_name='analysisrequestitem',
            name='pricing_rule',
            field=models.ForeignKey(
                blank=True,
                help_text='The pricing rule that was applied, if any. Kept for traceability.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='item_snapshots',
                to='catalog.pricingrule',
            ),
        ),
    ]
