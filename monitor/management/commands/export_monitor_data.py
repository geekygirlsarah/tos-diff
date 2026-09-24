"""
Management command: export_monitor_data

Usage:
    python manage.py export_monitor_data                          # write monitor_data.json
    python manage.py export_monitor_data --output full.json       # custom file path
    python manage.py export_monitor_data --no-snapshots           # skip snapshot rows

Writes the tracked organizations, documents, and snapshots (plus the
Country/Language/Tag lookup rows they depend on) to a JSON archive that can
be imported on another database with `import_monitor_data`. Records are keyed
by natural keys (slugs / codes / names) so the archive is a portable,
merge-friendly snapshot rather than a raw primary-key dump.
"""

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from django.utils.text import slugify

from monitor.models import (
    Country,
    Document,
    DocumentSnapshot,
    Language,
    Organization,
    Tag,
)

EXPORT_VERSION = 1


class _FullPrecisionJSONEncoder(DjangoJSONEncoder):
    """DjangoJSONEncoder truncates datetimes to milliseconds; keep microseconds."""

    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


class Command(BaseCommand):
    help = "Export tracked organizations, documents, and snapshots to a JSON archive."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--output",
            default="monitor_data.json",
            help="Path to write the JSON archive to (default: monitor_data.json).",
        )
        parser.add_argument(
            "--no-snapshots",
            action="store_true",
            help="Exclude DocumentSnapshot rows to keep the archive small.",
        )

    def handle(self, *args, **options) -> None:
        output = Path(options["output"])
        include_snapshots = not options["no_snapshots"]

        orgs = list(Organization.objects.order_by("id").prefetch_related("tags"))
        slug_by_pk = {org.pk: org.slug or slugify(org.name) for org in orgs}

        tag_by_pk = dict(Tag.objects.values_list("pk", "name"))
        tags_by_org: dict[int, list[str]] = defaultdict(list)
        for org_id, tag_id in Organization.tags.through.objects.values_list(
            "organization_id", "tag_id"
        ):
            name = tag_by_pk.get(tag_id)
            if name is not None:
                tags_by_org[org_id].append(name)

        countries = (self._country_row(c) for c in Country.objects.order_by("code").iterator())
        languages = (
            self._language_row(lang) for lang in Language.objects.order_by("code").iterator()
        )
        tags = (self._tag_row(t) for t in Tag.objects.order_by("name").iterator())
        organizations = (self._organization_row(org, slug_by_pk, tags_by_org) for org in orgs)
        documents = (
            self._document_row(doc, slug_by_pk)
            for doc in Document.objects.order_by("id")
            .select_related("language", "country")
            .iterator()
        )
        snapshots = (
            self._snapshot_row(snap, slug_by_pk)
            for snap in DocumentSnapshot.objects.order_by("document_id", "pk")
            .select_related("document")
            .iterator(chunk_size=2000)
        )

        sections = [
            ("countries", countries),
            ("languages", languages),
            ("tags", tags),
            ("organizations", organizations),
            ("documents", documents),
            ("snapshots", snapshots if include_snapshots else []),
        ]

        with output.open("w", encoding="utf-8") as fh:
            fh.write("{\n")
            fh.write(f'  "version": {EXPORT_VERSION},\n')
            fh.write(
                f'  "exported_at": {json.dumps(timezone.now(), cls=_FullPrecisionJSONEncoder)},\n'
            )
            for index, (name, rows) in enumerate(sections):
                if index:
                    fh.write(",\n")
                self._write_array(fh, name, rows)
            fh.write("\n}\n")

        self.stdout.write(self.style.SUCCESS(f"Exported monitor data to {output}"))

    # ------------------------------------------------------------------
    # Serializers
    # ------------------------------------------------------------------

    def _write_array(self, fh, name: str, rows) -> None:
        fh.write(f'  "{name}": [')
        first = True
        for row in rows:
            if first:
                fh.write("\n    ")
                first = False
            else:
                fh.write(",\n    ")
            fh.write(json.dumps(row, cls=_FullPrecisionJSONEncoder))
        if not first:
            fh.write("\n  ")
        fh.write("]")

    @staticmethod
    def _country_row(country: Country) -> dict:
        return {"pk": country.pk, "name": country.name, "code": country.code}

    @staticmethod
    def _language_row(language: Language) -> dict:
        return {"pk": language.pk, "name": language.name, "code": language.code}

    @staticmethod
    def _tag_row(tag: Tag) -> dict:
        return {"pk": tag.pk, "name": tag.name, "slug": tag.slug}

    @staticmethod
    def _organization_row(
        org: Organization,
        slug_by_pk: dict[int, str],
        tags_by_org: dict[int, list[str]],
    ) -> dict:
        parent_slug = slug_by_pk.get(org.parent_id) if org.parent_id is not None else None
        return {
            "pk": org.pk,
            "name": org.name,
            "slug": slug_by_pk[org.pk],
            "website_url": org.website_url,
            "category": org.category,
            "is_failing": org.is_failing,
            "parent": parent_slug,
            "tags": sorted(tags_by_org.get(org.pk, [])),
        }

    @staticmethod
    def _document_row(doc: Document, slug_by_pk: dict[int, str]) -> dict:
        return {
            "pk": doc.pk,
            "organization": slug_by_pk[doc.organization_id],
            "name": doc.name,
            "document_type": doc.document_type,
            "other_document_type": doc.other_document_type,
            "url": doc.url,
            "fetch_method": doc.fetch_method,
            "document_format": doc.document_format,
            "language": doc.language.code if doc.language_id else None,
            "country": doc.country.code if doc.country_id else None,
            "last_checked": doc.last_checked,
            "last_changed": doc.last_changed,
            "is_active": doc.is_active,
            "is_failing": doc.is_failing,
            "custom_selectors": doc.custom_selectors,
            "fetch_config": doc.fetch_config,
        }

    @staticmethod
    def _snapshot_row(snap: DocumentSnapshot, slug_by_pk: dict[int, str]) -> dict:
        return {
            "organization": slug_by_pk[snap.document.organization_id],
            "document_type": snap.document.document_type,
            "url": snap.document.url,
            "captured_at": snap.captured_at,
            "text_hash": snap.text_hash,
            "cleaned_text": snap.cleaned_text,
        }
