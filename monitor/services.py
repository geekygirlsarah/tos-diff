"""
Core fetching and snapshot logic — designed to be async-ready (Celery-friendly).
Each function is a pure, side-effect-free unit that can be called from a
management command, a Celery task, or a view.
"""

import hashlib
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag
from django.utils import timezone

from .models import Document, DocumentSnapshot

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_HEADERS = {
    "User-Agent": (
        "TosDiff-Monitor/1.0 (document change tracker; "
        "contact your-email@example.com)"
    )
}

# Tags always stripped before extraction
_STRIP_TAGS = [
    "script", "style", "noscript", "head",
    "nav", "footer", "iframe", "aside", "form",
]

# Heading level → markdown prefix
_HEADING_PREFIX = {
    "h1": "# ", "h2": "## ", "h3": "### ",
    "h4": "#### ", "h5": "##### ", "h6": "###### ",
}


def fetch_html(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch raw HTML from *url* using the requests library."""
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def _node_to_lines(tag: Tag) -> list[str]:
    """
    Recursively walk *tag* and return a list of text lines in
    markdown-like format.  Inline elements (strong, em, a, span, …)
    are handled inline; block elements each produce their own line(s).
    """
    INLINE_TAGS = {"strong", "b", "em", "i", "a", "span", "code", "abbr", "time"}
    BLOCK_TAGS = {
        "p", "div", "section", "article", "main", "header",
        "blockquote", "pre", "table", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "br", "hr",
    }

    lines: list[str] = []

    def _inline_text(node: Tag) -> str:
        """Collect inline text, wrapping strong/em in markdown markers."""
        parts: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                parts.append(child)
            elif isinstance(child, Tag):
                name = child.name
                inner = _inline_text(child)
                if name in ("strong", "b"):
                    parts.append(f"**{inner}**")
                elif name in ("em", "i"):
                    parts.append(f"_{inner}_")
                else:
                    parts.append(inner)
        return "".join(parts)

    def _walk(node: Tag) -> None:
        if isinstance(node, str):
            # Bare text node at top level — append to last line or start new
            text = node
            stripped = text.strip()
            if stripped:
                if lines and lines[-1] != "":
                    lines[-1] += " " + stripped
                else:
                    lines.append(stripped)
            return

        if not isinstance(node, Tag):
            return

        name = node.name

        # Headings
        if name in _HEADING_PREFIX:
            text = _inline_text(node).strip()
            if text:
                lines.append(f"{_HEADING_PREFIX[name]}{text}")
            lines.append("")
            return

        # List items
        if name == "li":
            text = _inline_text(node).strip()
            if text:
                lines.append(f"- {text}")
            return

        # Horizontal rule
        if name == "hr":
            lines.append("---")
            lines.append("")
            return

        # Line break
        if name == "br":
            lines.append("")
            return

        # Inline elements — render inline with markdown markers
        if name in INLINE_TAGS:
            inner = _inline_text(node).strip()
            if inner:
                if name in ("strong", "b"):
                    text = f"**{inner}**"
                elif name in ("em", "i"):
                    text = f"_{inner}_"
                else:
                    text = inner
                if lines and lines[-1] != "":
                    lines[-1] += " " + text
                else:
                    lines.append(text)
            return

        # Block elements — recurse, then ensure trailing blank line
        if name in BLOCK_TAGS:
            before = len(lines)
            for child in node.children:
                _walk(child)
            # Add blank line after block if content was added
            if len(lines) > before and lines and lines[-1] != "":
                lines.append("")
            return

        # Unknown / container — just recurse
        for child in node.children:
            _walk(child)

    for child in tag.children:
        _walk(child)

    return lines


def extract_text(
    html: str,
    extra_selectors: Optional[list[str]] = None,
) -> str:
    """
    Convert raw HTML to a clean, markdown-like plain-text representation
    suitable for diffing.

    Structural conversions
    ----------------------
    - ``h1``–``h6``  →  ``#``–``######`` prefixed lines
    - ``ul``/``ol``/``li``  →  ``- `` bullet lines
    - ``strong``/``b``  →  ``**text**``
    - ``em``/``i``  →  ``_text_``
    - ``hr``  →  ``---``

    Always stripped
    ---------------
    ``script``, ``style``, ``noscript``, ``head``, ``nav``, ``footer``,
    ``iframe``, ``aside``, ``form``

    Parameters
    ----------
    html:
        Raw HTML string to process.
    extra_selectors:
        Optional list of CSS selectors whose matching elements will be
        removed before extraction.  Use this for per-site customisation,
        e.g. ``[".cookie-banner", "#sidebar"]``.

    Returns
    -------
    str
        Normalised, whitespace-collapsed markdown-like text.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove always-stripped tags
    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    # Remove per-site custom selectors
    if extra_selectors:
        for selector in extra_selectors:
            for el in soup.select(selector):
                el.decompose()

    # Find the best content root: <main>, <article>, or <body>
    root: Tag = (
        soup.find("main")
        or soup.find("article")
        or soup.find("body")
        or soup
    )

    lines = _node_to_lines(root)

    # Normalise: strip each line, collapse consecutive blank lines
    normalised: list[str] = []
    prev_blank = False
    for raw_line in lines:
        # Collapse internal whitespace within a line
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line == "":
            if not prev_blank:
                normalised.append("")
            prev_blank = True
        else:
            normalised.append(line)
            prev_blank = False

    return "\n".join(normalised).strip()


# Keep the old name as an alias so existing call-sites don't break
def clean_html(html: str) -> str:
    """Thin wrapper around :func:`extract_text` for backwards compatibility."""
    return extract_text(html)


def compute_hash(text: str) -> str:
    """Return a SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_document_content(document: Document) -> Optional[str]:
    """
    Fetch and clean the content for *document*.

    Returns cleaned text, or None if fetching fails.
    Raises NotImplementedError for unsupported fetch methods.
    """
    if document.fetch_method == Document.FetchMethod.PLAYWRIGHT:
        raise NotImplementedError(
            "Playwright fetch method is not yet implemented. "
            "Set fetch_method to 'requests' for now."
        )

    # Parse per-document custom selectors (one per line, blank lines ignored)
    extra_selectors: list[str] = [
        s.strip()
        for s in (document.custom_selectors or "").splitlines()
        if s.strip()
    ]

    try:
        html = fetch_html(document.url)
        return extract_text(html, extra_selectors=extra_selectors or None)
    except requests.RequestException as exc:
        logger.error("Failed to fetch %s: %s", document.url, exc)
        return None


def create_snapshot_if_changed(document: Document, cleaned_text: str) -> tuple[Optional[DocumentSnapshot], bool]:
    """
    Compare *cleaned_text* against the latest snapshot for *document*.

    - If content has changed (or no snapshot exists), create and return a new
      DocumentSnapshot and update document timestamps.
    - Returns (snapshot, created) where *created* is True when a new snapshot
      was saved.
    """
    new_hash = compute_hash(cleaned_text)

    latest = document.snapshots.first()
    if latest is not None and latest.text_hash == new_hash:
        # Content unchanged — update last_checked only
        Document.objects.filter(pk=document.pk).update(last_checked=timezone.now())
        return None, False

    snapshot = DocumentSnapshot.objects.create(
        document=document,
        cleaned_text=cleaned_text,
        text_hash=new_hash,
    )
    now = timezone.now()
    Document.objects.filter(pk=document.pk).update(
        last_checked=now,
        last_changed=now,
    )
    return snapshot, True


def fetch_and_snapshot(document: Document) -> tuple[Optional[DocumentSnapshot], bool]:
    """
    High-level entry point: fetch content for *document* and snapshot if changed.

    Returns (snapshot, created).  snapshot is None when content is unchanged or
    fetching failed.
    """
    cleaned_text = fetch_document_content(document)
    if cleaned_text is None:
        return None, False
    return create_snapshot_if_changed(document, cleaned_text)
