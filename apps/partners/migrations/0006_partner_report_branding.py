from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('partners', '0005_remove_partnerorganization_invoice_vat_rate'),
    ]

    operations = [
        migrations.AddField(
            model_name='partnerorganization',
            name='custom_report_branding_enabled',
            field=models.BooleanField(
                default=False,
                help_text='When enabled, result PDFs for requests from this '
                          'partner use the partner-specific header/logo/footer '
                          'instead of the laboratory branding.',
            ),
        ),
        migrations.AddField(
            model_name='partnerorganization',
            name='report_header_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='partnerorganization',
            name='report_header_subtitle',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='partnerorganization',
            name='report_header_address',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='partnerorganization',
            name='report_header_phone',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
        migrations.AddField(
            model_name='partnerorganization',
            name='report_header_email',
            field=models.EmailField(blank=True, default='', max_length=254),
        ),
        migrations.AddField(
            model_name='partnerorganization',
            name='report_header_logo',
            field=models.ImageField(
                blank=True,
                help_text='Partner logo printed on result PDFs. PNG or JPEG, '
                          'recommended at least 600px wide for crisp rendering.',
                null=True,
                upload_to='partners/branding/logos/',
            ),
        ),
        migrations.AddField(
            model_name='partnerorganization',
            name='report_footer_text',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Confidentiality / legal text printed at the bottom of '
                          'result PDFs in place of the lab footer.',
            ),
        ),
    ]
