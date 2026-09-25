"""
Core fetching and snapshot logic — designed for synchronous use from the
daily management command or a view.
Each function is a pure, side-effect-free unit that can be called from a
management command, a task, or a view.
"""

import hashlib
import io
import logging
import queue
import random
import re
import secrets
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import pdfplumber
import requests
from bs4 import BeautifulSoup, Tag
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from .models import Document, DocumentSnapshot

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_HEADERS = {
    "User-Agent": ("TosDiff-Monitor/1.0 (document change tracker; contact your-email@example.com)")
}

# Last-resort User-Agent used only when the site bot-blocks our transparent
# TosDiff identity.  It matches the Chromium build bundled with this project's
# Playwright (see tests) so the TLS/version fingerprint stays consistent.
REAL_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Tags always stripped before extraction
_STRIP_TAGS = [
    "script",
    "style",
    "noscript",
    "head",
    "nav",
    "footer",
    "iframe",
    "aside",
    "form",
]

# Heading level → markdown prefix
_HEADING_PREFIX = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}


# ---------------------------------------------------------------------------
# Rate limiting — max 1 request per second per domain
# ---------------------------------------------------------------------------

_domain_last_fetch: dict[str, float] = defaultdict(float)
_domain_lock: dict[str, threading.Lock] = defaultdict(threading.Lock)
_domain_registry_lock = threading.Lock()
RATE_LIMIT_SECONDS = 1.0


def _get_domain(url: str) -> str:
    """Extract the netloc (domain) from *url*."""
    from urllib.parse import urlparse

    return urlparse(url).netloc


def _rate_limit(url: str) -> None:
    """
    Block until at least RATE_LIMIT_SECONDS have elapsed since the last fetch
    for the same domain.
    """
    domain = _get_domain(url)
    with _domain_registry_lock:
        if domain not in _domain_lock:
            _domain_lock[domain] = threading.Lock()
        lock = _domain_lock[domain]

    with lock:
        elapsed = time.monotonic() - _domain_last_fetch[domain]
        wait = RATE_LIMIT_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)
        # Add a small random jitter (0-0.5s) to make requests less predictable
        time.sleep(random.uniform(0, 0.5))
        _domain_last_fetch[domain] = time.monotonic()


# ---------------------------------------------------------------------------
# Playwright browser — one shared browser, owned by a single worker thread
# ---------------------------------------------------------------------------

# Chromium launch flags for a headless scraper:
#   --disable-dev-shm-usage      /dev/shm is tiny (64MB) in Docker; without this, large pages crash
#   --no-sandbox                 Chromium can't use its SUID sandbox when running as root in a container
#   --disable-gpu / --disable-software-rasterizer --headless
#                                reduce per-page GPU/shader memory
_CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-software-rasterizer",
]

_playwright_lock = threading.Lock()
_playwright_instance = None  # playwright context manager (owned by worker thread)
_playwright_browser = None  # Browser instance (owned by worker thread)

_playwright_worker_lock = threading.Lock()
_playwright_worker: threading.Thread | None = None
_playwright_queue: queue.Queue | None = None


# Resource types that never contribute to the extracted text but load
# full-resolution content into the renderer.  Aborting them keeps peak
# memory low while JS-heavy pages still render their markup.
_BLOCKED_RESOURCE_TYPES = ("image", "media", "font")


def _block_heavy_resources(route: Any) -> None:
    """Abort image/media/font requests; allow everything else through."""
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _get_playwright_browser() -> Any:
    """Return the shared, lazily-initialised Playwright Chromium browser.

    One browser is created per process and reused for every fetch, so a whole
    `fetch_documents` run uses a single Chromium instead of
    launching/closing one per document.  Must only be called from the Playwright
    worker thread (see :func:`_playwright_submit`).
    """
    global _playwright_instance, _playwright_browser
    with _playwright_lock:
        if _playwright_browser is None or not _playwright_browser.is_connected():
            from playwright.sync_api import sync_playwright

            _playwright_instance = sync_playwright().__enter__()
            _playwright_browser = _playwright_instance.chromium.launch(
                headless=True,
                args=_CHROMIUM_ARGS,
            )
    return _playwright_browser


def _playwright_worker_main() -> None:
    """
    Own the Playwright sync session for the lifetime of the process.

    Playwright's sync API leaves an asyncio event loop "running" in whatever
    thread executes it; Django's async-safety checks then reject every database
    call made from that thread.  All Playwright work is therefore confined to
    this single thread.  Running it here (rather than a fresh thread per fetch)
    keeps one browser alive and reused across submissions.
    """
    global _playwright_instance, _playwright_browser, _playwright_queue
    try:
        while True:
            holder = _playwright_queue.get()
            if holder is None:  # shutdown sentinel
                break
            func = holder["func"]
            try:
                holder["result"] = func()
            except BaseException as exc:  # noqa: BLE001
                holder["error"] = exc
            finally:
                holder["done"].set()
                _playwright_queue.task_done()
    finally:
        with _playwright_lock:
            if _playwright_browser is not None:
                try:
                    _playwright_browser.close()
                except Exception:  # noqa: BLE001
                    pass
                _playwright_browser = None
            if _playwright_instance is not None:
                try:
                    _playwright_instance.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                _playwright_instance = None


def _playwright_submit(func: Callable[[], Any]) -> Any:
    """
    Run *func* on the dedicated Playwright worker thread and block for its result.

    The worker (and its browser) is created lazily on first use and kept alive so
    consecutive fetches reuse a single Chromium instance.  Exceptions raised by
    *func* are re-raised in the calling thread.
    """
    global _playwright_worker, _playwright_queue
    with _playwright_worker_lock:
        if _playwright_worker is None or not _playwright_worker.is_alive():
            _playwright_queue = queue.Queue()
            _playwright_worker = threading.Thread(
                target=_playwright_worker_main,
                name="playwright-worker",
                daemon=True,
            )
            _playwright_worker.start()
        work_queue = _playwright_queue

    done = threading.Event()
    holder: dict[str, Any] = {"func": func, "done": done}
    work_queue.put(holder)
    done.wait()
    if "error" in holder:
        raise holder["error"]
    return holder["result"]


def close_playwright_browser() -> None:
    """Cleanly shut down the shared Playwright browser and its worker thread."""
    global _playwright_worker, _playwright_queue
    with _playwright_worker_lock:
        worker = _playwright_worker
        work_queue = _playwright_queue
        _playwright_worker = None
        _playwright_queue = None
    if work_queue is not None:
        work_queue.put(None)  # worker releases the browser, then stops
    if worker is not None:
        worker.join(timeout=10)


# ---------------------------------------------------------------------------
# HTTP fetch helpers
# ---------------------------------------------------------------------------


def fetch_html(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch raw HTML from *url* using the requests library."""
    _rate_limit(url)
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# Cloudflare verification challenges — detect and wait out before extracting
# ---------------------------------------------------------------------------

# URL substrings that indicate the page is a Cloudflare challenge (the page
# auto-redirects to the real URL once the browser-side checks pass).
_CHALLENGE_URL_MARKERS = (
    "cdn-cgi/challenge-platform",
    "cf_chl_",
    "challenges.cloudflare.com",
)

# Text that Cloudflare challenge pages commonly display while verifying.
_CHALLENGE_TEXT_MARKERS = (
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies",
)

# CSS selector for the interactive Turnstile checkbox iframe.  We only detect
# its presence; we never automate clicking it.
_TURNSTILE_IFRAME = "iframe[src*='challenges.cloudflare.com'], iframe[src*='cf_captcha']"

# Maximum seconds to wait for a challenge to auto-resolve before giving up.
DEFAULT_CHALLENGE_TIMEOUT = 30.0

# Per-poll window for wait_for_load_state while a challenge is pending.
_CHALLENGE_POLL_SECONDS = 10.0


def _looks_like_challenge(page: Any) -> bool:
    """Return True if *page* is showing a Cloudflare verification challenge."""
    if any(marker in str(page.url) for marker in _CHALLENGE_URL_MARKERS):
        return True
    try:
        turnstile_count = page.locator(_TURNSTILE_IFRAME).count()
        body_text = str(page.locator("body").inner_text(timeout=2000)).lower()
    except Exception:  # noqa: BLE001 - a mid-navigation page can raise; treat as not-challenge
        return False
    if isinstance(turnstile_count, int) and turnstile_count > 0:
        return True
    return any(marker in body_text for marker in _CHALLENGE_TEXT_MARKERS)


def _wait_for_challenge_to_clear(page: Any, challenge_timeout: float) -> None:
    """Wait up to *challenge_timeout* seconds for a challenge page to resolve.

    Cloudflare's non-interactive challenge runs fingerprinting checks in the
    browser and then redirects to the real page.  While the challenge is
    detected we keep polling: each poll waits up to ``_CHALLENGE_POLL_SECONDS``
    for a load event so a late redirect has time to complete.  If the challenge
    never clears within the deadline, we raise instead of extracting HTML that
    would otherwise be persisted as a (false-positive) document change.
    """
    deadline = time.monotonic() + challenge_timeout
    while time.monotonic() < deadline and _looks_like_challenge(page):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=_CHALLENGE_POLL_SECONDS * 1000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    if _looks_like_challenge(page):
        raise RuntimeError("Cloudflare challenge did not resolve within timeout")


def fetch_html_playwright(
    url: str,
    wait_for_selector: str | None = None,
    sleep_seconds: float = 0.0,
    dismiss_selectors: list[str] | None = None,
    challenge_timeout: float = DEFAULT_CHALLENGE_TIMEOUT,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str | None = None,
) -> str:
    """
    Fetch fully-rendered HTML from *url* using a shared headless Chromium browser.

    All work runs on a single dedicated worker thread (see
    :func:`_playwright_submit`) so Django never sees Playwright's event loop and
    one browser is reused for the whole run instead of launching a fresh
    Chromium per call.  A new :class:`BrowserContext` is created per fetch so
    cookies/storage never leak between pages.

    Parameters
    ----------
    url:
        Page URL to load.
    wait_for_selector:
        CSS selector to wait for before extracting HTML (e.g. ``"main"``).  If
        *None*, waits for ``"domcontentloaded"`` only.
    sleep_seconds:
        Additional seconds to sleep after the page has loaded, to allow
        late-rendering JavaScript to finish.
    dismiss_selectors:
        List of CSS selectors for cookie-consent / modal dismiss buttons to
        click before extracting HTML.
    challenge_timeout:
        Maximum seconds to wait for a Cloudflare verification challenge to
        auto-resolve before raising :class:`RuntimeError`.
    timeout:
        Navigation timeout in seconds.
    user_agent:
        User-Agent to present.  Defaults to the transparent
        :data:`DEFAULT_HEADERS` identity.
    """
    _rate_limit(url)

    def _run() -> str:
        browser = _get_playwright_browser()
        context = browser.new_context(
            user_agent=user_agent or DEFAULT_HEADERS["User-Agent"],
            java_script_enabled=True,
        )
        try:
            context.route("**/*", _block_heavy_resources)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

            _wait_for_challenge_to_clear(page, challenge_timeout)

            if wait_for_selector:
                page.wait_for_selector(wait_for_selector, timeout=timeout * 1000)

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

            for selector in dismiss_selectors or []:
                try:
                    btn = page.query_selector(selector)
                    if btn:
                        btn.click()
                        time.sleep(0.5)
                except Exception:  # noqa: BLE001
                    pass

            return page.content()
        finally:
            context.close()

    return _playwright_submit(_run)


def _is_pdf_response(response: requests.Response) -> bool:
    """Return True if *response* looks like a PDF (Content-Type or URL extension)."""
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/pdf" in content_type:
        return True
    url_path = response.url.split("?")[0].lower()
    return url_path.endswith(".pdf")


def fetch_pdf_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bytes, bool]:
    """
    Fetch *url* and return ``(content_bytes, is_pdf)``.

    *is_pdf* is True when the response is detected as a PDF.
    """
    _rate_limit(url)
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.content, _is_pdf_response(response)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """
    Extract and clean text from *pdf_bytes* using pdfplumber.

    Applies the same normalisation as :func:`extract_text`:
    - Collapse internal whitespace within each line
    - Remove consecutive blank lines
    - Strip lines that appear on every page (headers/footers/page numbers)

    Returns a normalised plain-text string.
    """
    page_texts: list[str] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            page_texts.append(text)

    # Collect per-page lines to detect repeated header/footer lines
    per_page_lines: list[list[str]] = []
    for raw in page_texts:
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        per_page_lines.append(lines)

    # A line is a repeated header/footer if it appears on more than half the pages
    # (only meaningful when there are at least 2 pages)
    repeated: set[str] = set()
    if len(per_page_lines) >= 2:
        from collections import Counter

        line_counts: Counter[str] = Counter()
        for page_lines in per_page_lines:
            for ln in set(page_lines):  # count once per page
                if ln:
                    line_counts[ln] += 1
        threshold = len(per_page_lines) / 2
        repeated = {ln for ln, count in line_counts.items() if count > threshold}

    # Also treat bare page-number lines as noise (e.g. "1", "Page 2", "- 3 -")
    _PAGE_NUM_RE = re.compile(r"^[-–—\s]*(?:page\s*)?\d+[-–—\s]*$", re.IGNORECASE)

    normalised: list[str] = []
    prev_blank = False
    for page_lines in per_page_lines:
        for ln in page_lines:
            line = re.sub(r"[ \t]+", " ", ln).strip()
            if line in repeated:
                continue
            if _PAGE_NUM_RE.match(line):
                continue
            if line == "":
                if not prev_blank:
                    normalised.append("")
                prev_blank = True
            else:
                normalised.append(line)
                prev_blank = False
        # Blank line between pages
        if not prev_blank:
            normalised.append("")
            prev_blank = True

    return "\n".join(normalised).strip()


def _node_to_lines(tag: Tag) -> list[str]:
    """
    Recursively walk *tag* and return a list of text lines in
    markdown-like format.  Inline elements (strong, em, a, span, …)
    are handled inline; block elements each produce their own line(s).
    """
    INLINE_TAGS = {"strong", "b", "em", "i", "a", "span", "code", "abbr", "time"}
    BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "blockquote",
        "pre",
        "table",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "br",
        "hr",
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
    extra_selectors: list[str] | None = None,
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
    root: Tag = soup.find("main") or soup.find("article") or soup.find("body") or soup

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


# ---------------------------------------------------------------------------
# Bot-block / access-denied pages — detect so they're marked failing instead
# of being stored as (false-positive) document changes
# ---------------------------------------------------------------------------

# Phrases that indicate the request was denied by an edge/CDN bot firewall
# (Akamai, Cloudflare, …) rather than a real document.
_BOT_BLOCK_DENIAL_MARKERS = (
    "access denied",
    "access has been denied",
    "access is denied",
    "you don't have permission",
    "you do not have permission",
    "request has been blocked",
    "the request has been blocked",
    "you have been blocked",
)

# Error-reference markers the block page carries so operators can look it up.
# Requiring one of these alongside a denial phrase stops genuine documents
# that merely mention "access" from being misclassified.
_BOT_BLOCK_REFERENCE_MARKERS = (
    "errors.edgesuite.net",
    "reference #",
    "error reference",
    "incident id",
    "cf-ray",
)


def _looks_like_bot_block(text: str) -> bool:
    """Return True when *text* is an access-denied / bot-block interstitial.

    Requires both a denial phrase and an error-reference marker so legitimate
    documents mentioning "access" are not misclassified.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _BOT_BLOCK_DENIAL_MARKERS) and any(
        marker in lowered for marker in _BOT_BLOCK_REFERENCE_MARKERS
    )


# HTTP status codes that indicate we should retry with Playwright
_PLAYWRIGHT_RETRY_STATUSES = {403, 429}


def fetch_document_content(document: Document) -> tuple[str | None, str]:
    """
    Fetch and clean the content for *document*.

    Strategy
    --------
    1. ``fetch_method`` selects the starting backend (and UA identity).
    2. If that attempt is bot-blocked / returns a retryable status / empty
       content, escalate to the next rung on the ladder (requests → Playwright
       with the transparent TosDiff identity → Playwright with a realistic
       browser UA).
    3. The method that succeeded is returned as the second element of the
       tuple so the caller can persist it; future runs then start directly on
       the winning rung instead of poking the site with multiple identities.

    Automatically detects PDF responses and extracts text accordingly.
    Sets ``document.document_format`` and saves it.

    Access-denied / bot-block interstitials (e.g. Akamai "Access Denied" pages
    carrying an error reference) are never treated as document content.  When
    the block page persists across the whole ladder the document is marked
    ``is_failing`` so the daily run stops polling it.

    Returns ``(cleaned_text, fetch_method_used)``.
    ``cleaned_text`` is *None* when fetching/extraction fails entirely.
    """
    # Parse per-document custom selectors (one per line, blank lines ignored)
    extra_selectors: list[str] = [
        s.strip() for s in (document.custom_selectors or "").splitlines() if s.strip()
    ]

    # Playwright config from fetch_config JSON field
    cfg = document.fetch_config or {}
    wait_for_selector: str | None = cfg.get("wait_for_selector")
    sleep_seconds: float = float(cfg.get("sleep_seconds", 0))
    dismiss_selectors: list[str] = cfg.get("dismiss_selectors") or []
    challenge_timeout: float = float(cfg.get("challenge_timeout", DEFAULT_CHALLENGE_TIMEOUT))

    # Ordered fallback ladder of (fetch_method) attempts for this document.
    # Plain requests presents the transparent identity and steps up to
    # Playwright (transparent, then a realistic browser UA) when blocked;
    # plain Playwright starts transparent and escalates to the browser UA;
    # ``playwright_browser`` always uses the browser UA directly.
    if document.fetch_method == Document.FetchMethod.PLAYWRIGHT_BROWSER:
        ladder = [Document.FetchMethod.PLAYWRIGHT_BROWSER]
    elif document.fetch_method == Document.FetchMethod.PLAYWRIGHT:
        ladder = [
            Document.FetchMethod.PLAYWRIGHT,
            Document.FetchMethod.PLAYWRIGHT_BROWSER,
        ]
    else:
        ladder = [
            Document.FetchMethod.REQUESTS,
            Document.FetchMethod.PLAYWRIGHT,
            Document.FetchMethod.PLAYWRIGHT_BROWSER,
        ]

    for method in ladder:
        if method == Document.FetchMethod.REQUESTS:
            # ------------------------------------------------------------------
            # Attempt: plain HTTP requests with the transparent identity
            # ------------------------------------------------------------------
            try:
                _rate_limit(document.url)
                response = requests.get(
                    document.url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT
                )
                if response.status_code in _PLAYWRIGHT_RETRY_STATUSES:
                    logger.info(
                        "fetch_document_content: %s returned %s — will retry with Playwright",
                        document.url,
                        response.status_code,
                    )
                    continue
                response.raise_for_status()
                if _is_pdf_response(response):
                    Document.objects.filter(pk=document.pk).update(
                        document_format=Document.DocumentFormat.PDF
                    )
                    document.document_format = Document.DocumentFormat.PDF
                    try:
                        text = extract_pdf_text(response.content)
                        return text or None, Document.FetchMethod.REQUESTS
                    except Exception as exc:
                        logger.error("Failed to extract PDF text from %s: %s", document.url, exc)
                        return None, Document.FetchMethod.REQUESTS

                html = response.content.decode("utf-8", errors="replace")
                text = extract_text(html, extra_selectors=extra_selectors or None)
                if _looks_like_bot_block(text):
                    logger.warning(
                        "fetch_document_content: %s returned an access-denied / "
                        "bot-block page — will retry with Playwright",
                        document.url,
                    )
                    continue
                if text:
                    Document.objects.filter(pk=document.pk).update(
                        document_format=Document.DocumentFormat.HTML
                    )
                    document.document_format = Document.DocumentFormat.HTML
                    return text, Document.FetchMethod.REQUESTS

                # Empty content — fall back to Playwright
                logger.info(
                    "fetch_document_content: empty content from %s — will retry with Playwright",
                    document.url,
                )
                continue
            except requests.RequestException as exc:
                logger.error("Failed to fetch %s via requests: %s", document.url, exc)
                continue
        else:
            # ------------------------------------------------------------------
            # Attempt: Playwright headless browser.  ``playwright_browser``
            # presents the realistic browser User-Agent; plain Playwright
            # presents the transparent TosDiff identity.
            # ------------------------------------------------------------------
            user_agent = (
                REAL_BROWSER_USER_AGENT
                if method == Document.FetchMethod.PLAYWRIGHT_BROWSER
                else DEFAULT_HEADERS["User-Agent"]
            )
            try:
                html = fetch_html_playwright(
                    document.url,
                    wait_for_selector=wait_for_selector,
                    sleep_seconds=sleep_seconds,
                    dismiss_selectors=dismiss_selectors,
                    challenge_timeout=challenge_timeout,
                    user_agent=user_agent,
                )
                text = extract_text(html, extra_selectors=extra_selectors or None)
            except Exception as exc:
                logger.error("Failed to fetch %s via Playwright: %s", document.url, exc)
                return None, method
            if _looks_like_bot_block(text):
                logger.warning(
                    "fetch_document_content: %s returned an access-denied / bot-block "
                    "page via Playwright (%s)",
                    document.url,
                    "browser UA"
                    if method == Document.FetchMethod.PLAYWRIGHT_BROWSER
                    else "TosDiff UA",
                )
                continue
            Document.objects.filter(pk=document.pk).update(
                document_format=Document.DocumentFormat.HTML
            )
            document.document_format = Document.DocumentFormat.HTML
            return text or None, method

    # Every rung on the ladder came back as a bot-block page — mark the
    # document failing so the daily run stops polling it and the changing
    # error-reference values never produce false change digests.
    logger.warning(
        "fetch_document_content: %s still bot-blocked after exhausting fetch "
        "ladder — marking document as failing",
        document.url,
    )
    Document.objects.filter(pk=document.pk).update(
        is_failing=True,
        last_checked=timezone.now(),
    )
    document.is_failing = True
    return None, ladder[-1]


def create_snapshot_if_changed(
    document: Document, cleaned_text: str
) -> tuple[DocumentSnapshot | None, bool]:
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


def fetch_and_snapshot(document: Document) -> tuple[DocumentSnapshot | None, bool]:
    """
    High-level entry point: fetch content for *document* and snapshot if changed.

    Tries plain HTTP requests first; falls back to Playwright on 403/429 or
    empty content.  Persists the successful ``fetch_method`` back to the
    document so future runs use the right method directly.

    Returns (snapshot, created).  snapshot is None when content is unchanged or
    fetching failed.
    """
    cleaned_text, method_used = fetch_document_content(document)

    # Persist the method that worked (or was attempted last)
    if method_used != document.fetch_method:
        Document.objects.filter(pk=document.pk).update(fetch_method=method_used)
        document.fetch_method = method_used

    if cleaned_text is None:
        return None, False
    return create_snapshot_if_changed(document, cleaned_text)


# ---------------------------------------------------------------------------
# Authentication — passwordless one-time login codes
# ---------------------------------------------------------------------------

LOGIN_CODE_TTL_MINUTES = 10


def generate_login_code() -> str:
    """Return a random, six-digit, zero-padded one-time login code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def send_login_code_email(email: str, code: str, ttl_minutes: int = LOGIN_CODE_TTL_MINUTES) -> None:
    """Email a one-time login *code* to *email*."""
    subject = "Your TosDiff login code"
    message = (
        "Hi,\n\n"
        f"Your one-time TosDiff login code is: {code}\n\n"
        f"It expires in {ttl_minutes} minutes.\n\n"
        "If you didn't request this code, you can safely ignore this email."
    )
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email])


# ---------------------------------------------------------------------------
# Change notification emails
# ---------------------------------------------------------------------------

UNSUBSCRIBE_SALT = "tosdiff.unsubscribe"
UNSUBSCRIBE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year


def make_unsubscribe_token(user_id: int, document_id: int) -> str:
    """Sign a (user, document) pair so it can be turned into an email link."""
    return signing.dumps(
        {"user_id": user_id, "document_id": document_id},
        salt=UNSUBSCRIBE_SALT,
    )


def read_unsubscribe_token(token: str) -> dict[str, int]:
    """
    Unpack a signed unsubscribe *token*.

    Raises ``BadSignature`` when the token is invalid or too old.
    """
    payload = signing.loads(token, salt=UNSUBSCRIBE_SALT, max_age=UNSUBSCRIBE_MAX_AGE)
    return {"user_id": int(payload["user_id"]), "document_id": int(payload["document_id"])}


def send_suggestion_review_email(
    suggestion,
    *,
    approved: bool,
    site_url: str,
    review_notes: str = "",
    document=None,
) -> None:
    """
    Email ``suggestion.contact_email`` with the outcome of a review.

    *approved* is True when the suggestion was converted into a tracked
    organization+document (*document* must be the created ``Document`` then);
    False when it was rejected.  Does nothing when no contact email is set.
    """
    if not suggestion.contact_email:
        return
    base = site_url.rstrip("/")
    org_name = suggestion.organization_name
    if approved:
        url = base + reverse("monitor:document_detail", args=[document.pk])
        subject = f"[TosDiff] Your suggestion for {org_name} was approved"
        plain_lines = [
            f"Great news — your suggestion for {org_name} was approved and is now being tracked.",
            "",
            f"Tracked document: {document.display_name}",
            "",
            f"View it here: {url}",
        ]
        html = (
            "<html><body>"
            f"<p>Great news — your suggestion for <strong>{org_name}</strong> "
            "was approved and is now being tracked.</p>"
            f"<p>Tracked document: {document.display_name}<br>"
            f'<a href="{url}">View it here</a></p>'
            "</body></html>"
        )
    else:
        subject = f"[TosDiff] Your suggestion for {org_name} was not approved"
        plain_lines = [
            f"Thank you for suggesting {org_name}.",
            "",
            "After review, this document isn't being added to TosDiff at this time.",
        ]
        html = (
            "<html><body>"
            f"<p>Thank you for suggesting <strong>{org_name}</strong>.</p>"
            "<p>After review, this document isn't being added to TosDiff at this time.</p>"
        )
        if review_notes:
            plain_lines += ["", f"Note from the reviewer: {review_notes}"]
            html += f"<p>Note from the reviewer: {review_notes}</p>"
        html += "</body></html>"

    body = "\n".join(plain_lines)
    send_mail(
        subject, body, settings.DEFAULT_FROM_EMAIL, [suggestion.contact_email], html_message=html
    )
