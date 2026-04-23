"""Add per-tenant monthly LabelSequence counter."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('analysis_requests', '0011_analysisrequest_final_conclusion_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='LabelSequence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('year', models.PositiveSmallIntegerField()),
                ('month', models.PositiveSmallIntegerField()),
                ('last_value', models.PositiveIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Label Sequence',
                'verbose_name_plural': 'Label Sequences',
            },
        ),
        migrations.AddConstraint(
            model_name='labelsequence',
            constraint=models.UniqueConstraint(
                fields=('year', 'month'),
                name='unique_label_sequence_year_month',
            ),
        ),
        migrations.AddIndex(
            model_name='labelsequence',
            index=models.Index(fields=['year', 'month'],
                               name='analysis_re_year_332e90_idx'),
        ),
    ]
