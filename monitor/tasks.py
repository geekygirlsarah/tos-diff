"""
Celery tasks for document fetching.

Workers:  celery -A tosdiff_new worker -l info
Beat:     celery -A tosdiff_new beat -l info
"""

import logging
import random

import requests
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from celery.signals import worker_shutdown
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail

from .models import (
    Document,
    DocumentSnapshot,
    DocumentSubscription,
    OrganizationSubscription,
)
from .services import (
    build_snapshot_change_message,
    close_playwright_browser,
    fetch_and_snapshot,
)


@worker_shutdown.connect
def _close_playwright_on_shutdown(**kwargs) -> None:
    """Release the shared Playwright browser when the Celery worker stops."""
    close_playwright_browser()


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single-document task
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute between retries
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,  # exponential back-off
    retry_backoff_max=600,  # cap at 10 minutes
    retry_jitter=True,
    acks_late=True,
    name="monitor.tasks.check_document",
)
def check_document(self, document_id: int) -> dict:
    """
    Fetch and snapshot a single Document.

    Returns a dict with keys: document_id, created, error.
    Retries automatically on network errors (up to max_retries).
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
    except requests.RequestException as exc:
        logger.warning(
            "check_document: network error for document %s: %s — retrying",
            document_id,
            exc,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error("check_document: max retries exceeded for document %s", document_id)
            return {"document_id": document_id, "created": False, "error": str(exc)}
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
        _dispatch_change_notifications(doc, snapshot)
    else:
        logger.info("check_document: no change detected for document %s", document_id)

    return {"document_id": document_id, "created": created, "error": None}


def _dispatch_change_notifications(document: Document, snapshot: DocumentSnapshot) -> None:
    """Queue notification emails if anyone subscribes to *document* or its org."""
    has_subscribers = DocumentSubscription.objects.filter(document=document).exists() or (
        OrganizationSubscription.objects.filter(organization=document.organization).exists()
    )
    if has_subscribers:
        send_change_notifications.delay(document.pk, snapshot.pk)


# ---------------------------------------------------------------------------
# Change notification task
# ---------------------------------------------------------------------------


@shared_task(name="monitor.tasks.send_change_notifications")
def send_change_notifications(document_id: int, new_snapshot_id: int) -> dict:
    """
    Email every subscriber of *document_id* about the new snapshot.

    A user is notified if they subscribe to the document directly or to the
    document's organization. Each user receives at most one email.
    """
    try:
        document = Document.objects.select_related("organization").get(pk=document_id)
    except Document.DoesNotExist:
        logger.error("send_change_notifications: Document %s not found", document_id)
        return {"sent": 0, "error": "Document not found"}

    snapshot = document.snapshots.filter(pk=new_snapshot_id).first()
    if snapshot is None:
        logger.error(
            "send_change_notifications: Snapshot %s not found for document %s",
            new_snapshot_id,
            document_id,
        )
        return {"sent": 0, "error": "Snapshot not found"}

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
        return {"sent": 0, "error": None}

    subject, plain_body, html_body = build_snapshot_change_message(
        document, snapshot, old_snapshot, settings.BASE_URL
    )

    sent = 0
    for user in recipients:
        send_mail(
            subject,
            plain_body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_body,
        )
        sent += 1

    logger.info(
        "send_change_notifications: sent %d notification(s) for document %s",
        sent,
        document_id,
    )
    return {"sent": sent, "error": None}


# ---------------------------------------------------------------------------
# Periodic task — dispatched by Celery Beat at 02:00 UTC
# ---------------------------------------------------------------------------


@shared_task(name="monitor.tasks.check_all_documents")
def check_all_documents() -> dict:
    """
    Enqueue a check_document task for every active and non-failing Document.
    Runs daily at 02:00 UTC via Celery Beat.
    """
    ids = list(
        Document.objects.filter(
            is_active=True,
            is_failing=False,
            organization__is_failing=False,
        ).values_list("pk", flat=True)
    )
    if len(ids) > 1:
        random.shuffle(ids)
    for doc_id in ids:
        check_document.delay(doc_id)
    logger.info("check_all_documents: enqueued %d document(s)", len(ids))
    return {"enqueued": len(ids)}
