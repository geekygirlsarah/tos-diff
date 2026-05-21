from django.db import migrations, models
import django.db.models.deletion


def create_english_and_assign(apps, schema_editor):
    Language = apps.get_model("monitor", "Language")
    Document = apps.get_model("monitor", "Document")
    english, _ = Language.objects.get_or_create(code="en", defaults={"name": "English"})
    Document.objects.update(language=english)


class Migration(migrations.Migration):

    dependencies = [
        ("monitor", "0002_document_custom_selectors"),
    ]

    operations = [
        # 1. Create the Language model
        migrations.CreateModel(
            name="Language",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text='Display name, e.g. "English", "Français"', max_length=100)),
                ("code", models.CharField(help_text='ISO 639-1 code, e.g. "en", "fr"', max_length=10, unique=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        # 2. Expand DocumentType choices (no schema change needed — choices are validated in Python only)
        migrations.AlterField(
            model_name="document",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("tos", "Terms of Service"),
                    ("privacy", "Privacy Policy"),
                    ("cookie", "Cookie Policy"),
                    ("refund", "Refund Policy"),
                    ("childrens_privacy", "Children's Privacy Policy"),
                    ("subscription", "Subscription Policy"),
                    ("service_agreement", "Service Agreement"),
                    ("service_fees", "Service Fees"),
                    ("user_agreement", "User Agreement"),
                    ("conduct", "Code of Conduct / Community Standards"),
                    ("acceptable_use", "Acceptable Use Policy"),
                    ("dmca", "DMCA / Copyright Policy"),
                    ("other", "Other"),
                ],
                default="tos",
                max_length=20,
            ),
        ),
        # 3. Add nullable language FK
        migrations.AddField(
            model_name="document",
            name="language",
            field=models.ForeignKey(
                blank=True,
                help_text="Language this document is written in.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="monitor.language",
            ),
        ),
        # 4. Seed English and assign to all existing documents
        migrations.RunPython(create_english_and_assign, migrations.RunPython.noop),
    ]
