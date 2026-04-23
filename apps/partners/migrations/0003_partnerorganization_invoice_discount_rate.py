"""Add invoice_discount_rate to PartnerOrganization."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('partners', '0002_partnerexamprice_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='partnerorganization',
            name='invoice_discount_rate',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=5, null=True,
                help_text='Global invoice discount percentage (e.g. 10.00 for 10%). '
                          'Applied on the gross total of generated invoices. '
                          'Distinct from per-exam negotiated prices.',
            ),
        ),
    ]
