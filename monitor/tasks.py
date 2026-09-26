"""
Document checking and change-notification dispatch.

This module exposes plain, synchronous functions that are driven by
the ``fetch_documents`` management command (the daily cron job)
and by the admin "check now" action.
"""

import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    Document,
    DocumentSnapshot,
    DocumentSubscription,
    NotificationPreference,
    OrganizationSubscription,
    PendingNotification,
)
from .services import (
    fetch_and_snapshot,
    make_unsubscribe_token,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single-document check
# ---------------------------------------------------------------------------


def check_document(document_id: int) -> dict:
    """
    Fetch and snapshot a single Document.

    Returns a dict with keys: document_id, created, error.
    """
    try:
        doc = Document.objects.select_related("organization").get(pk=document_id)
    except Document.DoesNotExist:
        logger.error("check_document: Document %s not found", document_id)
        return {"document_id": document_id, "created": False, "error": "Document not found"}

    if not doc.is_active:
        logger.info("check_document: Document %s is inactive, skipping", document_id)
        return {"document_id": document_id, "created": False, "error": None}

    if doc.is_failing:
        logger.info("check_document: Document %s is marked as failing, skipping", document_id)
        return {"document_id": document_id, "created": False, "error": "Document marked as failing"}

    if doc.organization.is_failing:
        logger.info(
            "check_document: Organization %s is marked as failing, skipping document %s",
            doc.organization.name,
            document_id,
        )
        return {
            "document_id": document_id,
            "created": False,
            "error": "Organization marked as failing",
        }

    logger.info("check_document: fetching document %s (%s)", document_id, doc.url)

    try:
        snapshot, created = fetch_and_snapshot(doc)
    except NotImplementedError as exc:
        logger.error(
            "check_document: fetch method not implemented for document %s: %s",
            document_id,
            exc,
        )
        return {"document_id": document_id, "created": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("check_document: unexpected error for document %s", document_id)
        return {"document_id": document_id, "created": False, "error": str(exc)}

    if created:
        logger.info("check_document: new snapshot created for document %s", document_id)
        dispatch_change_notifications(doc, snapshot)
    else:
        logger.info("check_document: no change detected for document %s", document_id)

    return {"document_id": document_id, "created": created, "error": None}


def dispatch_change_notifications(document: Document, snapshot: DocumentSnapshot) -> None:
    """Queue PendingNotification rows if anyone subscribes to *document* or its org."""
    has_subscribers = DocumentSubscription.objects.filter(document=document).exists() or (
        OrganizationSubscription.objects.filter(organization=document.organization).exists()
    )
    if has_subscribers:
        send_change_notifications(document.pk, snapshot.pk)


# ---------------------------------------------------------------------------
# Change notification queueing
# ---------------------------------------------------------------------------


def send_change_notifications(document_id: int, new_snapshot_id: int) -> dict:
    """
    Queue a change notification for every subscriber of *document_id*.

    A user is notified if they subscribe to the document directly or to the
    document's organization. Each user receives at most one notification.
    Subscribers are delivered the update via their daily or weekly digest
    (a missing preference is treated as daily), so the change is only queued
    here rather than emailed directly.

    Returns ``{"sent": ..., "queued": ..., "error": ...}``.
    """
    try:
        document = Document.objects.select_related("organization").get(pk=document_id)
    except Document.DoesNotExist:
        logger.error("send_change_notifications: Document %s not found", document_id)
        return {"sent": 0, "queued": 0, "error": "Document not found"}

    snapshot = document.snapshots.filter(pk=new_snapshot_id).first()
    if snapshot is None:
        logger.error(
            "send_change_notifications: Snapshot %s not found for document %s",
            new_snapshot_id,
            document_id,
        )
        return {"sent": 0, "queued": 0, "error": "Snapshot not found"}

    old_snapshot = document.snapshots.exclude(pk=new_snapshot_id).first()

    user_ids = set(
        DocumentSubscription.objects.filter(document=document).values_list("user_id", flat=True)
    )
    user_ids.update(
        OrganizationSubscription.objects.filter(organization=document.organization).values_list(
            "user_id", flat=True
        )
    )

    recipients = list(
        get_user_model().objects.filter(pk__in=user_ids, is_active=True).exclude(email="")
    )
    if not recipients:
        return {"sent": 0, "queued": 0, "error": None}

    queued = 0
    for user in recipients:
        PendingNotification.objects.create(
            user=user,
            document=document,
            snapshot=snapshot,
            old_snapshot=old_snapshot,
        )
        queued += 1

    logger.info(
        "send_change_notifications: queued %d notification(s) for document %s",
        queued,
        document_id,
    )
    return {"sent": 0, "queued": queued, "error": None}


# ---------------------------------------------------------------------------
# Digest emails
# ---------------------------------------------------------------------------


def _send_digest(frequency: str, digest_label: str) -> dict:
    """
    Email one digest to each user whose preference is *frequency* with any
    pending notifications, then clear their queue.

    Users without a ``NotificationPreference`` row are treated as daily.
    """
    pref_q = Q(notification_preference__frequency=frequency)
    if frequency == NotificationPreference.Frequency.DAILY:
        pref_q |= Q(notification_preference__isnull=True)
    users = get_user_model().objects.filter(pref_q, is_active=True).exclude(email="")
    sent = 0
    base = settings.BASE_URL.rstrip("/")
    for user in users:
        pending = list(
            user.pending_notifications.select_related(
                "document__organization", "snapshot", "old_snapshot"
            )
        )
        if not pending:
            continue

        pending.sort(
            key=lambda n: (
                n.document.organization.name.lower(),
                n.document.display_name.lower(),
            )
        )

        plain_lines = [
            "Here's a summary of documents that changed:",
            "",
        ]
        html_items: list[str] = []
        for notification in pending:
            doc = notification.document
            detail_url = base + reverse("monitor:document_detail", args=[doc.pk])
            unsubscribe_url = base + reverse(
                "monitor:unsubscribe_token",
                args=[make_unsubscribe_token(user.pk, doc.pk)],
            )
            captured = notification.snapshot.captured_at
            plain_lines.append(
                f"- {doc.organization.name} — {doc.display_name} "
                f"(captured {captured:%Y-%m-%d %H:%M UTC}): {detail_url}"
            )
            plain_lines.append(f"  Unsubscribe: {unsubscribe_url}")
            html_items.append(
                f"<li><strong>{doc.organization.name}</strong> — {doc.display_name} "
                f"({captured:%Y-%m-%d %H:%M UTC}) — "
                f'<a href="{detail_url}">view document</a> — '
                f'<a href="{unsubscribe_url}">unsubscribe</a></li>'
            )
        plain_lines += [
            "",
            "You're receiving this because you subscribed to updates. "
            "Manage your subscriptions in your TosDiff account.",
        ]

        subject = f"[TosDiff] {digest_label} digest — {len(pending)} document change(s)"
        html_body = (
            "<html><body>"
            f"<p>Here's a summary of documents that changed:</p>"
            f"<ul>{''.join(html_items)}</ul>"
            '<p style="color:#64748b;font-size:12px;">'
            "You're receiving this because you subscribed to updates. "
            "Manage your subscriptions in your TosDiff account.</p>"
            "</body></html>"
        )
        send_mail(
            subject,
            "\n".join(plain_lines),
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_body,
        )
        user.pending_notifications.all().delete()
        sent += 1

    logger.info("_send_digest (%s): sent %d digest(s)", digest_label, sent)
    return {"sent": sent, "error": None}


def send_daily_digests() -> dict:
    """Email daily digest subscribers the changes queued since the last run."""
    return _send_digest(NotificationPreference.Frequency.DAILY, "Daily")


def send_weekly_digests() -> dict:
    """Email weekly digest subscribers the changes queued since the last run."""
    return _send_digest(NotificationPreference.Frequency.WEEKLY, "Weekly")


def is_weekly_digest_day(now: datetime | None = None) -> bool:
    """True when *now* (default: now) lands on the weekly digest weekday.

    The weekday is ``settings.WEEKLY_DIGEST_WEEKDAY`` (ISO: 1=Monday … 7=Sunday).
    """
    now = now or timezone.localtime()
    return now.isoweekday() == settings.WEEKLY_DIGEST_WEEKDAY
