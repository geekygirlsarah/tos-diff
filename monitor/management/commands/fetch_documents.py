"""
Management command: fetch_documents

Usage:
    python manage.py fetch_documents                  # fetch all active documents synchronously
    python manage.py fetch_documents --id 3           # fetch a single document by PK
    python manage.py fetch_documents --dry-run        # print what would be fetched
    python manage.py fetch_documents --async          # dispatch Celery tasks instead of running inline
    python manage.py fetch_documents --async --id 3   # dispatch a single Celery task
"""

from django.core.management.base import BaseCommand, CommandError

from monitor.models import Document
from monitor.services import fetch_and_snapshot


class Command(BaseCommand):
    help = "Fetch documents and create snapshots when content has changed."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--id",
            type=int,
            dest="document_id",
            help="Fetch only the document with this primary key.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List documents that would be fetched without actually fetching.",
        )
        parser.add_argument(
            "--async",
            action="store_true",
            dest="use_celery",
            help="Dispatch Celery tasks instead of running synchronously (requires a running worker).",
        )

    def handle(self, *args, **options) -> None:
        document_id: int | None = options["document_id"]
        dry_run: bool = options["dry_run"]
        use_celery: bool = options["use_celery"]

        qs = Document.objects.select_related("organization").filter(is_active=True)
        if document_id is not None:
            qs = qs.filter(pk=document_id)
            if not qs.exists():
                raise CommandError(f"No active document found with id={document_id}.")

        if not qs.exists():
            self.stdout.write(self.style.WARNING("No active documents found."))
            return

        total = qs.count()
        self.stdout.write(f"Found {total} document(s) to process.")

        if dry_run:
            for doc in qs:
                self.stdout.write(f"  [dry-run] Would fetch: {doc} — {doc.url}")
            return

        if use_celery:
            self._dispatch_celery(qs)
        else:
            self._run_sync(qs, total)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dispatch_celery(self, qs) -> None:
        """Enqueue a check_document Celery task for each document."""
        from monitor.tasks import check_document  # import here to avoid hard dep when Celery not installed

        enqueued = 0
        for doc in qs:
            check_document.delay(doc.pk)
            self.stdout.write(f"  [async] Enqueued task for: {doc}")
            enqueued += 1
        self.stdout.write(self.style.SUCCESS(f"\nEnqueued {enqueued} task(s). Make sure a Celery worker is running."))

    def _run_sync(self, qs, total: int) -> None:
        """Fetch each document synchronously in the current process."""
        changed = 0
        errors = 0

        for doc in qs:
            self.stdout.write(f"  Fetching: {doc} …", ending=" ")
            try:
                _snapshot, created = fetch_and_snapshot(doc)
                if created:
                    self.stdout.write(self.style.SUCCESS("CHANGED"))
                    changed += 1
                else:
                    self.stdout.write(self.style.HTTP_NOT_MODIFIED("unchanged"))
            except NotImplementedError as exc:
                self.stdout.write(self.style.WARNING(f"SKIPPED ({exc})"))
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"ERROR: {exc}"))
                errors += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {changed}/{total} document(s) changed. {errors} error(s)."
            )
        )
