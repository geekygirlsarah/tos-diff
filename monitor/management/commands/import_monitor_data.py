"""
Management command: import_monitor_data

Usage:
    python manage.py import_monitor_data                          # read monitor_data.json
    python manage.py import_monitor_data --input full.json        # custom file path
    python manage.py import_monitor_data --no-snapshots           # skip snapshot rows
    python manage.py import_monitor_data --replace                # wipe monitored data first

Loads a JSON archive produced by `export_monitor_data` into the current
database. Records are matched by natural keys (Country/Language code, Tag
name, Organization slug, Document's (organization, type, url) tuple) and
snapshots by (document, text_hash, captured_at), so importing twice is
idempotent and merges into existing rows instead of duplicating them.

`--replace` first deletes all monitored data in the current database
(snapshots, documents, organizations, tags, countries, languages) so the
database ends up mirroring the archive exactly. Deleting organizations
also cascades to user DocumentSubscription / OrganizationSubscription /
PendingNotification rows, so use it only when you really want to reset.
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Case, F, Value, When
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from monitor.models import (
    Country,
    Document,
    DocumentSnapshot,
    Language,
    Organization,
    Tag,
)

SNAPSHOT_BATCH_SIZE = 500
SNAPSHOT_PROGRESS_BATCHES = 4
ROW_PROGRESS_INTERVAL = 200


class Command(BaseCommand):
    help = "Import a monitor data JSON archive into the current database."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--input",
            default="monitor_data.json",
            help="Path to the JSON archive to import (default: monitor_data.json).",
        )
        parser.add_argument(
            "--no-snapshots",
            action="store_true",
            help="Skip DocumentSnapshot rows even if the archive contains them.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "Delete all existing monitored data (snapshots, documents, "
                "organizations, tags, countries, languages) before importing so the "
                "database mirrors the archive. Cascades to user subscriptions and "
                "pending notifications."
            ),
        )

    def handle(self, *args, **options) -> None:
        source = Path(options["input"])
        include_snapshots = not options["no_snapshots"]

        if not source.exists():
            raise CommandError(f"Input file not found: {source}")

        with source.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        if data.get("version") is None:
            self.stdout.write(
                self.style.WARNING("Archive has no version field; assuming it is compatible.")
            )

        with transaction.atomic():
            if options["replace"]:
                self._wipe_existing()
            counts = self._import_lookup_tables(data)
            counts.update(self._import_organizations(data))
            counts.update(self._import_documents(data))
            if include_snapshots:
                counts["snapshots"] = self._import_snapshots(data)
            else:
                counts["snapshots"] = 0
                self.stdout.write(self.style.WARNING("Skipping snapshots."))

        self.stdout.write(self.style.SUCCESS("Import complete."))
        for label, count in counts.items():
            self.stdout.write(f"  {label}: {count}")

    # ------------------------------------------------------------------
    # Import steps
    # ------------------------------------------------------------------

    def _wipe_existing(self) -> None:
        """Delete all data the archive manages so import mirrors it exactly."""
        self.stdout.write("Removing existing monitored data...")
        for label, queryset in (
            ("snapshots", DocumentSnapshot.objects.all()),
            ("documents", Document.objects.all()),
            ("organizations", Organization.objects.all()),
            ("tags", Tag.objects.all()),
            ("languages", Language.objects.all()),
            ("countries", Country.objects.all()),
        ):
            count = queryset.count()
            queryset.delete()
            self.stdout.write(f"  {label}: {count} removed")

    def _import_lookup_tables(self, data: dict) -> dict:
        for rec in data.get("countries", []):
            Country.objects.update_or_create(code=rec["code"], defaults={"name": rec["name"]})
        for rec in data.get("languages", []):
            Language.objects.update_or_create(code=rec["code"], defaults={"name": rec["name"]})
        for rec in data.get("tags", []):
            slug = rec.get("slug") or slugify(rec["name"])
            Tag.objects.update_or_create(name=rec["name"], defaults={"slug": slug})

        self.country_by_code = {c.code: c for c in Country.objects.all()}
        self.language_by_code = {lang.code: lang for lang in Language.objects.all()}
        return {
            "countries": len(self.country_by_code),
            "languages": len(self.language_by_code),
            "tags": Tag.objects.count(),
        }

    def _import_organizations(self, data: dict) -> dict:
        self.organization_by_slug: dict[str, Organization] = {}
        records = data.get("organizations", [])
        for index, rec in enumerate(records, start=1):
            organization, _ = Organization.objects.update_or_create(
                slug=rec["slug"],
                defaults={
                    "name": rec["name"],
                    "website_url": rec["website_url"],
                    "category": rec.get("category", ""),
                    "is_failing": rec.get("is_failing", False),
                },
            )
            self.organization_by_slug[rec["slug"]] = organization
            if index % ROW_PROGRESS_INTERVAL == 0:
                self.stdout.write(f"  organizations: {index:,} / {len(records):,}")

        for rec in data.get("organizations", []):
            organization = self.organization_by_slug[rec["slug"]]
            parent_slug = rec.get("parent")
            parent = self.organization_by_slug.get(parent_slug) if parent_slug else None
            if (
                parent is not None
                and parent.pk != organization.pk
                and organization.parent_id != parent.pk
            ):
                organization.parent = parent
                organization.save(update_fields=["parent"])

            tag_names = [name for name in rec.get("tags", [])]
            tags = [tag for tag in Tag.objects.filter(name__in=tag_names)]
            organization.tags.set(tags)

        return {"organizations": len(self.organization_by_slug)}

    def _import_documents(self, data: dict) -> dict:
        self.document_by_key: dict[tuple, Document] = {}
        records = data.get("documents", [])
        for index, rec in enumerate(records, start=1):
            organization = self.organization_by_slug.get(rec["organization"])
            if organization is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping document {rec['url']!r}: unknown organization "
                        f"{rec['organization']!r}."
                    )
                )
                continue

            language = self.language_by_code.get(rec.get("language"))
            country = self.country_by_code.get(rec.get("country"))
            document, _ = Document.objects.update_or_create(
                organization=organization,
                document_type=rec["document_type"],
                url=rec["url"],
                defaults={
                    "name": rec.get("name", ""),
                    "other_document_type": rec.get("other_document_type", ""),
                    "fetch_method": rec.get("fetch_method", Document.FetchMethod.REQUESTS),
                    "document_format": rec.get("document_format", Document.DocumentFormat.HTML),
                    "language": language,
                    "country": country,
                    "last_checked": self._parse_optional_datetime(rec.get("last_checked")),
                    "last_changed": self._parse_optional_datetime(rec.get("last_changed")),
                    "is_active": rec.get("is_active", True),
                    "is_failing": rec.get("is_failing", False),
                    "custom_selectors": rec.get("custom_selectors", ""),
                    "fetch_config": rec.get("fetch_config"),
                },
            )
            key = (rec["organization"], rec["document_type"], rec["url"])
            self.document_by_key[key] = document
            if index % ROW_PROGRESS_INTERVAL == 0:
                self.stdout.write(f"  documents: {index:,} / {len(records):,}")

        return {"documents": len(self.document_by_key)}

    def _import_snapshots(self, data: dict) -> int:
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for rec in data.get("snapshots", []):
            key = (rec["organization"], rec["document_type"], rec["url"])
            grouped[key].append(rec)

        total = sum(len(rows) for rows in grouped.values())
        imported = 0
        batch_no = 0
        for key, rows in grouped.items():
            document = self.document_by_key.get(key)
            if document is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping {len(rows)} snapshot(s) for unknown document "
                        f"({key[1]} / {key[2]})."
                    )
                )
                continue

            existing = set(
                DocumentSnapshot.objects.filter(document=document).values_list(
                    "text_hash", "captured_at"
                )
            )
            to_create = []
            for rec in rows:
                captured_at = parse_datetime(rec["captured_at"])
                if captured_at is None:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping snapshot with unparseable captured_at "
                            f"{rec['captured_at']!r}."
                        )
                    )
                    continue
                if (rec["text_hash"], captured_at) in existing:
                    continue
                to_create.append(
                    DocumentSnapshot(
                        document=document,
                        captured_at=captured_at,
                        text_hash=rec["text_hash"],
                        cleaned_text=rec["cleaned_text"],
                    )
                )

            for start in range(0, len(to_create), SNAPSHOT_BATCH_SIZE):
                batch = to_create[start : start + SNAPSHOT_BATCH_SIZE]
                captured_ats = [snap.captured_at for snap in batch]
                created = DocumentSnapshot.objects.bulk_create(batch)
                self._restore_captured_at(list(zip(created, captured_ats, strict=True)))
                imported += len(created)
                batch_no += 1
                if batch_no % SNAPSHOT_PROGRESS_BATCHES == 0:
                    self.stdout.write(f"  snapshots imported: {imported:,} / {total:,} snapshots")

        return imported

    @staticmethod
    def _restore_captured_at(rows: list[tuple[DocumentSnapshot, datetime]]) -> None:
        """bulk_create() re-applies auto_now_add, so rewrite captured_at afterwards."""
        DocumentSnapshot.objects.filter(pk__in=[snap.pk for snap, _ in rows]).update(
            captured_at=Case(
                *[When(pk=snap.pk, then=Value(captured_at)) for snap, captured_at in rows],
                default=F("captured_at"),
            )
        )

    @staticmethod
    def _parse_optional_datetime(value: str | None):
        if not value:
            return None
        return parse_datetime(value)
