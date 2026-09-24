"""
Management command: fetch_documents

Runs the full daily monitoring cycle in one process (no Celery/Redis):

    1. Fetch every active document and snapshot any that changed.
    2. For each changed document, queue a PendingNotification for every
       document/organization subscriber (daily and weekly digest users).
    3. Send the daily digests for eligible users.
    4. Send the weekly digests when the run lands on the weekly digest
       weekday (ISO weekday from WEEKLY_DIGEST_WEEKDAY, default Monday).

Usage:
    python manage.py fetch_documents                  # full daily cycle
    python manage.py fetch_documents --id 3           # single document only
    python manage.py fetch_documents --dry-run        # list what would be fetched
    python manage.py fetch_documents --skip-notifications   # fetch only
    python manage.py fetch_documents --skip-digests         # fetch + queue, no email
"""

import random

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from monitor.models import Document
from monitor.services import fetch_and_snapshot
from monitor.tasks import (
    dispatch_change_notifications,
    is_weekly_digest_day,
    send_daily_digests,
    send_weekly_digests,
)

WEEKDAY_NAMES = {
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
    7: "Sunday",
}


class Command(BaseCommand):
    help = "Fetch documents, queue change notifications, and send digests."

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
            "--no-shuffle",
            action="store_false",
            dest="shuffle",
            default=True,
            help="Disable shuffling of the document list before processing.",
        )
        parser.add_argument(
            "--include-failing",
            action="store_true",
            help="Fetch documents even if they or their organization are marked as failing.",
        )
        parser.add_argument(
            "--skip-notifications",
            action="store_true",
            help="Fetch and snapshot only; do not queue change notifications.",
        )
        parser.add_argument(
            "--skip-digests",
            action="store_true",
            help="Fetch and queue notifications, but do not send digest emails.",
        )

    def handle(self, *args, **options) -> None:
        document_id: int | None = options["document_id"]
        dry_run: bool = options["dry_run"]
        shuffle: bool = options["shuffle"]
        include_failing: bool = options["include_failing"]

        qs = Document.objects.select_related("organization").filter(is_active=True)
        if not include_failing:
            qs = qs.filter(is_failing=False, organization__is_failing=False)

        if document_id is not None:
            qs = qs.filter(pk=document_id)
            if not qs.exists():
                raise CommandError(f"No active document found with id={document_id}.")

        if not qs.exists():
            self.stdout.write(self.style.WARNING("No active documents found."))
            return

        docs = list(qs)
        total = len(docs)
        if shuffle and total > 1:
            self.stdout.write("Shuffling document list...")
            random.shuffle(docs)

        self.stdout.write(f"Found {total} document(s) to process.")

        if dry_run:
            for doc in docs:
                self.stdout.write(f"  [dry-run] Would fetch: {doc} — {doc.url}")
            return

        self._run_sync(docs, total, options)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_sync(self, docs, total: int, options: dict) -> None:
        """Fetch each document, queue notifications on change, then send digests."""
        skip_notifications: bool = options["skip_notifications"]
        skip_digests: bool = options["skip_digests"]
        changed = 0
        errors = 0

        for doc in docs:
            self.stdout.write(f"  Fetching: {doc} …", ending=" ")
            try:
                snapshot, created = fetch_and_snapshot(doc)
                if created:
                    self.stdout.write(self.style.SUCCESS("CHANGED"))
                    changed += 1
                    if not skip_notifications:
                        dispatch_change_notifications(doc, snapshot)
                else:
                    self.stdout.write(self.style.HTTP_NOT_MODIFIED("unchanged"))
            except NotImplementedError as exc:
                self.stdout.write(self.style.WARNING(f"SKIPPED ({exc})"))
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"ERROR: {exc}"))
                errors += 1

        from monitor.services import close_playwright_browser

        close_playwright_browser()

        self.stdout.write(
            self.style.SUCCESS(f"\nDone. {changed}/{total} document(s) changed. {errors} error(s).")
        )

        if not skip_digests:
            self._send_digests()

    def _send_digests(self) -> None:
        daily = send_daily_digests()
        self.stdout.write(f"Sent {daily['sent']} daily digest(s).")
        if is_weekly_digest_day():
            weekly = send_weekly_digests()
            self.stdout.write(f"Sent {weekly['sent']} weekly digest(s).")
        else:
            today = timezone.localtime()
            name = WEEKDAY_NAMES.get(today.isoweekday(), str(today))
            self.stdout.write(
                self.style.WARNING(
                    f"Not a weekly digest day (today is {name}); skipped weekly digests."
                )
            )
