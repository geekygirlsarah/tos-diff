"""
Celery tasks for document fetching.

Workers:  celery -A tosdiff_new worker -l info
Beat:     celery -A tosdiff_new beat -l info
"""

import logging

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from celery.signals import worker_shutdown

import requests

from .models import Document
from .services import close_playwright_browser, fetch_and_snapshot


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
    default_retry_delay=60,          # 1 minute between retries
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,              # exponential back-off
    retry_backoff_max=600,           # cap at 10 minutes
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

    logger.info("check_document: fetching document %s (%s)", document_id, doc.url)

    try:
        snapshot, created = fetch_and_snapshot(doc)
    except requests.RequestException as exc:
        logger.warning(
            "check_document: network error for document %s: %s — retrying",
            document_id, exc,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                "check_document: max retries exceeded for document %s", document_id
            )
            return {"document_id": document_id, "created": False, "error": str(exc)}
    except NotImplementedError as exc:
        logger.error(
            "check_document: fetch method not implemented for document %s: %s",
            document_id, exc,
        )
        return {"document_id": document_id, "created": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("check_document: unexpected error for document %s", document_id)
        return {"document_id": document_id, "created": False, "error": str(exc)}

    if created:
        logger.info("check_document: new snapshot created for document %s", document_id)
    else:
        logger.info("check_document: no change detected for document %s", document_id)

    return {"document_id": document_id, "created": created, "error": None}


# ---------------------------------------------------------------------------
# Periodic task — dispatched by Celery Beat at 02:00 UTC
# ---------------------------------------------------------------------------

@shared_task(name="monitor.tasks.check_all_documents")
def check_all_documents() -> dict:
    """
    Enqueue a check_document task for every active Document.
    Runs daily at 02:00 UTC via Celery Beat.
    """
    ids = list(Document.objects.filter(is_active=True).values_list("pk", flat=True))
    for doc_id in ids:
        check_document.delay(doc_id)
    logger.info("check_all_documents: enqueued %d document(s)", len(ids))
    return {"enqueued": len(ids)}
