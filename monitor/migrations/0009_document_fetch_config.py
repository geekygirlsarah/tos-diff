# Generated manually on 2026-05-20

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitor', '0008_document_format'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='fetch_config',
            field=models.JSONField(
                blank=True,
                null=True,
                default=None,
                help_text=(
                    'Optional Playwright fetch configuration. '
                    'Keys: wait_for_selector (CSS selector to wait for), '
                    'sleep_seconds (extra wait time for JS rendering), '
                    'dismiss_selectors (list of CSS selectors for cookie/modal buttons to click).'
                ),
            ),
        ),
    ]
