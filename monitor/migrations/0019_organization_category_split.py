"""Move organizations off the retired ``financial`` category.

``Organization.category`` is a plain ``CharField``, so rows already in the database keep
their raw value regardless of what ``Category`` now declares. Leaving them on
``"financial"`` would strand them on a value that fails validation — invisible in the
form dropdown and dropped by anything that reads ``get_category_display()``.

Every affected row is moved to ``banking`` rather than being blanked or deleted. Banking
is the largest of the three successor buckets by a wide margin, and the residue
(brokerages, card issuers) is a one-click fix in ``/manage/organizations/`` rather than a
lost row. The reverse is a no-op, since ``banking`` is valid both before and after.
"""

from django.db import migrations

RETIRED = "financial"
SUCCESSOR = "banking"


def forwards(apps, schema_editor):
    Organization = apps.get_model("monitor", "Organization")
    Organization.objects.filter(category=RETIRED).update(category=SUCCESSOR)


def backwards(apps, schema_editor):
    """Not reversible: we cannot tell which ``banking`` rows used to be ``financial``."""
    return None


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0018_organization_category_choices"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
