"""Add forensic audit fields to PasswordResetToken: used_at, created_by_ip."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0005_staffuser_phone'),
    ]

    operations = [
        migrations.AddField(
            model_name='passwordresettoken',
            name='used_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='Set when the token is consumed; complements is_used for audit/forensics.',
            ),
        ),
        migrations.AddField(
            model_name='passwordresettoken',
            name='created_by_ip',
            field=models.GenericIPAddressField(
                blank=True, null=True,
                help_text='IP address of the requester at token creation time. '
                          'Useful for forensic review of password-reset abuse.',
            ),
        ),
    ]
