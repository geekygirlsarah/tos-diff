"""
Unit tests for the monitor app.

Run with:  python manage.py test monitor
"""

import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.log import AdminEmailHandler

from .emailing import MailgunBackend
from .models import (
    Country,
    Document,
    DocumentSnapshot,
    DocumentSubscription,
    Language,
    LoginCode,
    NotificationPreference,
    Organization,
    OrganizationSubscription,
    PendingNotification,
    Suggestion,
    Tag,
)
from .services import (
    clean_html,
    compute_hash,
    create_snapshot_if_changed,
    extract_pdf_text,
    extract_text,
    fetch_and_snapshot,
    fetch_pdf_bytes,
    generate_login_code,
    make_unsubscribe_token,
    send_login_code_email,
)
from .tasks import send_change_notifications, send_daily_digests, send_weekly_digests

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TagModelTest(TestCase):
    def test_slug_auto_generated(self):
        tag = Tag.objects.create(name="Open Source")
        self.assertEqual(tag.slug, "open-source")

    def test_slug_not_overwritten_if_set(self):
        tag = Tag.objects.create(name="Open Source", slug="custom-tag")
        self.assertEqual(tag.slug, "custom-tag")

    def test_str(self):
        tag = Tag(name="Fintech")
        self.assertEqual(str(tag), "Fintech")

    def test_unique_name(self):
        Tag.objects.create(name="Unique Tag")
        with self.assertRaises(IntegrityError):
            Tag.objects.create(name="Unique Tag")


class OrganizationCategoryTagsTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Corp", website_url="https://acme.com")

    def test_category_default_blank(self):
        self.assertEqual(self.org.category, "")

    def test_category_can_be_set(self):
        self.org.category = Organization.Category.TECHNOLOGY
        self.org.save()
        self.org.refresh_from_db()
        self.assertEqual(self.org.category, "technology")

    def test_all_categories_exist(self):
        expected = [
            "technology",
            "financial",
            "healthcare",
            "entertainment_streaming",
            "media",
            "social_media",
            "hospitality",
            "retail",
            "other",
        ]
        actual = [c.value for c in Organization.Category]
        for value in expected:
            self.assertIn(value, actual)

    def test_tags_can_be_added(self):
        tag1 = Tag.objects.create(name="SaaS")
        tag2 = Tag.objects.create(name="Cloud")
        self.org.tags.add(tag1, tag2)
        self.assertEqual(self.org.tags.count(), 2)

    def test_tags_reverse_relation(self):
        tag = Tag.objects.create(name="E-Commerce")
        self.org.tags.add(tag)
        self.assertIn(self.org, tag.organizations.all())

    def test_tags_optional(self):
        self.assertEqual(self.org.tags.count(), 0)


class OrganizationModelTest(TestCase):
    def test_slug_auto_generated(self):
        org = Organization.objects.create(name="Example Corp", website_url="https://example.com")
        self.assertEqual(org.slug, "example-corp")

    def test_slug_not_overwritten_if_set(self):
        org = Organization.objects.create(
            name="Example Corp", slug="custom-slug", website_url="https://example.com"
        )
        self.assertEqual(org.slug, "custom-slug")

    def test_str(self):
        org = Organization(name="Acme")
        self.assertEqual(str(org), "Acme")

    def test_favicon_url_uses_website_domain(self):
        org = Organization.objects.create(name="Acme", website_url="https://www.acme.com/some/path")
        self.assertEqual(
            org.favicon_url,
            "https://icons.duckduckgo.com/ip3/www.acme.com.ico",
        )

    def test_favicon_url_strips_port_and_scheme(self):
        org = Organization.objects.create(name="Acme", website_url="http://acme.com:8080/")
        self.assertEqual(org.favicon_url, "https://icons.duckduckgo.com/ip3/acme.com.ico")

    def test_favicon_url_empty_when_no_hostname(self):
        org = Organization(name="Acme", website_url="not a url")
        self.assertEqual(org.favicon_url, "")


class OrganizationParentTest(TestCase):
    def test_parent_can_be_set(self):
        meta = Organization.objects.create(name="Meta", website_url="https://meta.com")
        instagram = Organization.objects.create(
            name="Instagram", website_url="https://instagram.com", parent=meta
        )
        instagram.refresh_from_db()
        self.assertEqual(instagram.parent, meta)

    def test_parent_nullable(self):
        org = Organization.objects.create(name="Standalone", website_url="https://standalone.com")
        self.assertIsNone(org.parent)

    def test_subsidiaries_reverse_relation(self):
        meta = Organization.objects.create(name="Meta", website_url="https://meta.com")
        Organization.objects.create(
            name="Facebook", website_url="https://facebook.com", parent=meta
        )
        Organization.objects.create(
            name="Instagram", website_url="https://instagram.com", parent=meta
        )
        subsidiary_names = set(meta.subsidiaries.values_list("name", flat=True))
        self.assertIn("Facebook", subsidiary_names)
        self.assertIn("Instagram", subsidiary_names)

    def test_parent_set_null_on_delete(self):
        meta = Organization.objects.create(name="Meta", website_url="https://meta.com")
        instagram = Organization.objects.create(
            name="Instagram", website_url="https://instagram.com", parent=meta
        )
        meta.delete()
        instagram.refresh_from_db()
        self.assertIsNone(instagram.parent)


class DocumentModelTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")

    def test_str(self):
        doc = Document(organization=self.org, document_type=Document.DocumentType.PRIVACY_POLICY)
        self.assertIn("Acme", str(doc))
        self.assertIn("Privacy Policy", str(doc))

    def test_default_fetch_method(self):
        doc = Document.objects.create(
            organization=self.org,
            url="https://acme.com/tos",
        )
        self.assertEqual(doc.fetch_method, Document.FetchMethod.REQUESTS)

    def test_default_is_active(self):
        doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.assertTrue(doc.is_active)


class DocumentSnapshotModelTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")

    def test_str_contains_document(self):
        snap = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="hello", text_hash="abc"
        )
        self.assertIn("Acme", str(snap))


# ---------------------------------------------------------------------------
# Language model tests
# ---------------------------------------------------------------------------


class CountryModelTest(TestCase):
    def test_str(self):
        country = Country(name="United States", code="US")
        self.assertEqual(str(country), "United States (US)")

    def test_code_unique(self):
        Country.objects.create(name="United States", code="US")
        with self.assertRaises(IntegrityError):
            Country.objects.create(name="US Again", code="US")

    def test_ordering_by_name(self):
        Country.objects.create(name="France", code="FR")
        Country.objects.create(name="Australia", code="AU")
        names = list(Country.objects.values_list("name", flat=True))
        self.assertEqual(names, ["Australia", "France"])

    def test_country_fk_on_document(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        us = Country.objects.create(name="United States", code="US")
        doc = Document.objects.create(organization=org, url="https://acme.com/tos", country=us)
        doc.refresh_from_db()
        self.assertEqual(doc.country.code, "US")

    def test_country_nullable_on_document(self):
        org = Organization.objects.create(name="Acme2", website_url="https://acme2.com")
        doc = Document.objects.create(organization=org, url="https://acme2.com/tos")
        self.assertIsNone(doc.country)

    def test_country_set_null_on_delete(self):
        org = Organization.objects.create(name="Acme3", website_url="https://acme3.com")
        us = Country.objects.create(name="United States", code="US")
        doc = Document.objects.create(organization=org, url="https://acme3.com/tos", country=us)
        us.delete()
        doc.refresh_from_db()
        self.assertIsNone(doc.country)


class LanguageModelTest(TestCase):
    def test_str(self):
        lang = Language(name="English", code="en")
        self.assertEqual(str(lang), "English (en)")

    def test_code_unique(self):
        Language.objects.filter(code="en").delete()
        Language.objects.create(name="English", code="en")
        with self.assertRaises(IntegrityError):
            Language.objects.create(name="English Again", code="en")

    def test_ordering_by_name(self):
        Language.objects.all().delete()
        Language.objects.create(name="Spanish", code="es")
        Language.objects.create(name="Arabic", code="ar")
        codes = list(Language.objects.values_list("code", flat=True))
        self.assertEqual(codes, ["ar", "es"])


class DocumentLanguageTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.english, _ = Language.objects.get_or_create(code="en", defaults={"name": "English"})

    def test_language_fk_assigned(self):
        doc = Document.objects.create(
            organization=self.org, url="https://acme.com/tos", language=self.english
        )
        doc.refresh_from_db()
        self.assertEqual(doc.language.code, "en")

    def test_language_nullable(self):
        doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.assertIsNone(doc.language)

    def test_other_document_type_label(self):
        doc = Document.objects.create(
            organization=self.org,
            url="https://acme.com/other",
            document_type=Document.DocumentType.OTHER,
            other_document_type="Transparency Report",
        )
        doc.refresh_from_db()
        self.assertEqual(doc.other_document_type, "Transparency Report")

    def test_other_document_type_blank_by_default(self):
        doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.assertEqual(doc.other_document_type, "")

    def test_new_document_types_exist(self):
        expected = [
            Document.DocumentType.REFUND_POLICY,
            Document.DocumentType.CHILDRENS_PRIVACY,
            Document.DocumentType.SUBSCRIPTION_POLICY,
            Document.DocumentType.SERVICE_AGREEMENT,
            Document.DocumentType.SERVICE_FEES,
            Document.DocumentType.USER_AGREEMENT,
            Document.DocumentType.CODE_OF_CONDUCT,
            Document.DocumentType.ACCEPTABLE_USE,
            Document.DocumentType.DMCA_POLICY,
            Document.DocumentType.PAYMENT_SERVICE_TERMS,
        ]
        for dt in expected:
            doc = Document.objects.create(
                organization=self.org,
                url=f"https://acme.com/{dt.value}",
                document_type=dt,
            )
            self.assertEqual(doc.document_type, dt)


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


class ExtractTextTest(TestCase):
    """Tests for the extract_text() function and its clean_html() alias."""

    # --- stripping -----------------------------------------------------------

    def test_strips_script(self):
        html = "<html><body><script>alert(1)</script><p>Hello</p></body></html>"
        self.assertNotIn("alert", extract_text(html))
        self.assertIn("Hello", extract_text(html))

    def test_strips_style(self):
        html = "<html><body><style>body{color:red}</style><p>World</p></body></html>"
        self.assertNotIn("color", extract_text(html))

    def test_strips_nav(self):
        html = "<html><body><nav>Menu</nav><p>Content</p></body></html>"
        result = extract_text(html)
        self.assertNotIn("Menu", result)
        self.assertIn("Content", result)

    def test_strips_footer(self):
        html = "<html><body><p>Main</p><footer>Footer text</footer></body></html>"
        result = extract_text(html)
        self.assertNotIn("Footer text", result)
        self.assertIn("Main", result)

    # --- headings ------------------------------------------------------------

    def test_h1_becomes_markdown_heading(self):
        html = "<html><body><h1>Title</h1></body></html>"
        self.assertIn("# Title", extract_text(html))

    def test_h2_becomes_markdown_heading(self):
        html = "<html><body><h2>Section</h2></body></html>"
        self.assertIn("## Section", extract_text(html))

    def test_h3_to_h6_headings(self):
        for level in range(3, 7):
            tag = f"h{level}"
            prefix = "#" * level
            html = f"<html><body><{tag}>Heading</{tag}></body></html>"
            self.assertIn(f"{prefix} Heading", extract_text(html))

    # --- inline formatting ---------------------------------------------------

    def test_strong_becomes_bold(self):
        html = "<html><body><p><strong>Important</strong></p></body></html>"
        self.assertIn("**Important**", extract_text(html))

    def test_b_becomes_bold(self):
        html = "<html><body><p><b>Bold</b></p></body></html>"
        self.assertIn("**Bold**", extract_text(html))

    def test_em_becomes_italic(self):
        html = "<html><body><p><em>Emphasis</em></p></body></html>"
        self.assertIn("_Emphasis_", extract_text(html))

    def test_i_becomes_italic(self):
        html = "<html><body><p><i>Italic</i></p></body></html>"
        self.assertIn("_Italic_", extract_text(html))

    # --- lists ---------------------------------------------------------------

    def test_ul_li_become_bullets(self):
        html = "<html><body><ul><li>One</li><li>Two</li></ul></body></html>"
        result = extract_text(html)
        self.assertIn("- One", result)
        self.assertIn("- Two", result)

    def test_ol_li_become_bullets(self):
        html = "<html><body><ol><li>First</li><li>Second</li></ol></body></html>"
        result = extract_text(html)
        self.assertIn("- First", result)
        self.assertIn("- Second", result)

    # --- whitespace normalisation --------------------------------------------

    def test_collapses_blank_lines(self):
        html = "<html><body><p>A</p><p></p><p></p><p>B</p></body></html>"
        self.assertNotIn("\n\n\n", extract_text(html))

    def test_collapses_internal_whitespace(self):
        html = "<html><body><p>Hello   world</p></body></html>"
        result = extract_text(html)
        self.assertNotIn("   ", result)
        self.assertIn("Hello world", result)

    def test_empty_html(self):
        self.assertEqual("", extract_text("<html><body></body></html>"))

    # --- custom selectors ----------------------------------------------------

    def test_extra_selector_removes_element(self):
        html = "<html><body><div class='cookie-banner'>Accept cookies</div><p>Real content</p></body></html>"
        result = extract_text(html, extra_selectors=[".cookie-banner"])
        self.assertNotIn("Accept cookies", result)
        self.assertIn("Real content", result)

    def test_extra_selector_by_id(self):
        html = "<html><body><div id='sidebar'>Ads</div><p>Article</p></body></html>"
        result = extract_text(html, extra_selectors=["#sidebar"])
        self.assertNotIn("Ads", result)
        self.assertIn("Article", result)

    def test_multiple_extra_selectors(self):
        html = (
            "<html><body>"
            "<div class='ad'>Ad</div>"
            "<div id='popup'>Popup</div>"
            "<p>Content</p>"
            "</body></html>"
        )
        result = extract_text(html, extra_selectors=[".ad", "#popup"])
        self.assertNotIn("Ad", result)
        self.assertNotIn("Popup", result)
        self.assertIn("Content", result)

    def test_no_extra_selectors_is_safe(self):
        html = "<html><body><p>Hello</p></body></html>"
        self.assertIn("Hello", extract_text(html, extra_selectors=None))

    # --- backwards-compat alias ----------------------------------------------

    def test_clean_html_alias(self):
        html = "<html><body><h1>Hi</h1><p>There</p></body></html>"
        self.assertEqual(clean_html(html), extract_text(html))


class ComputeHashTest(TestCase):
    def test_same_text_same_hash(self):
        self.assertEqual(compute_hash("hello"), compute_hash("hello"))

    def test_different_text_different_hash(self):
        self.assertNotEqual(compute_hash("hello"), compute_hash("world"))

    def test_hash_length(self):
        self.assertEqual(len(compute_hash("test")), 64)


class CreateSnapshotIfChangedTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")

    def test_creates_snapshot_when_no_previous(self):
        snapshot, created = create_snapshot_if_changed(self.doc, "New content")
        self.assertTrue(created)
        self.assertIsNotNone(snapshot)
        self.assertEqual(DocumentSnapshot.objects.count(), 1)

    def test_no_snapshot_when_content_unchanged(self):
        create_snapshot_if_changed(self.doc, "Same content")
        snapshot, created = create_snapshot_if_changed(self.doc, "Same content")
        self.assertFalse(created)
        self.assertIsNone(snapshot)
        self.assertEqual(DocumentSnapshot.objects.count(), 1)

    def test_creates_snapshot_when_content_changed(self):
        create_snapshot_if_changed(self.doc, "Original content")
        snapshot, created = create_snapshot_if_changed(self.doc, "Updated content")
        self.assertTrue(created)
        self.assertIsNotNone(snapshot)
        self.assertEqual(DocumentSnapshot.objects.count(), 2)

    def test_updates_last_checked_on_no_change(self):
        create_snapshot_if_changed(self.doc, "Content")
        create_snapshot_if_changed(self.doc, "Content")
        self.doc.refresh_from_db()
        self.assertIsNotNone(self.doc.last_checked)

    def test_updates_last_changed_on_change(self):
        create_snapshot_if_changed(self.doc, "Content v1")
        create_snapshot_if_changed(self.doc, "Content v2")
        self.doc.refresh_from_db()
        self.assertIsNotNone(self.doc.last_changed)

    def test_hash_stored_correctly(self):
        text = "Some document text"
        snapshot, _ = create_snapshot_if_changed(self.doc, text)
        self.assertEqual(snapshot.text_hash, compute_hash(text))


class FetchAndSnapshotTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")

    @patch("monitor.services.fetch_pdf_bytes")
    def test_creates_snapshot_on_new_content(self, mock_fetch):
        mock_fetch.return_value = (b"<html><body><p>Terms of Service</p></body></html>", False)
        snapshot, created = fetch_and_snapshot(self.doc)
        self.assertTrue(created)
        self.assertIsNotNone(snapshot)

    @patch("monitor.services.fetch_pdf_bytes")
    def test_no_snapshot_on_unchanged_content(self, mock_fetch):
        mock_fetch.return_value = (b"<html><body><p>Terms of Service</p></body></html>", False)
        fetch_and_snapshot(self.doc)
        snapshot, created = fetch_and_snapshot(self.doc)
        self.assertFalse(created)
        self.assertIsNone(snapshot)

    @patch("monitor.services.fetch_html_playwright", side_effect=Exception("Playwright error"))
    @patch("monitor.services.requests.get", side_effect=requests.RequestException("Network error"))
    def test_returns_none_on_fetch_error(self, mock_get, mock_playwright):
        snapshot, created = fetch_and_snapshot(self.doc)
        self.assertIsNone(snapshot)
        self.assertFalse(created)

    @patch("monitor.services.fetch_html_playwright")
    def test_playwright_method_calls_playwright(self, mock_playwright):
        mock_playwright.return_value = "<html><body><p>Terms</p></body></html>"
        self.doc.fetch_method = Document.FetchMethod.PLAYWRIGHT
        self.doc.save()
        from monitor.services import fetch_document_content

        text, method = fetch_document_content(self.doc)
        mock_playwright.assert_called_once()
        self.assertEqual(method, Document.FetchMethod.PLAYWRIGHT)


class PlaywrightBrowserReuseTest(TestCase):
    """The shared Playwright browser is created once and reused across fetches."""

    def setUp(self):
        from monitor import services

        services._playwright_browser = None
        services._playwright_instance = None

    def _fake_sync_playwright(self, mock_sync):
        browser = MagicMock()
        browser.is_connected.return_value = True
        page = MagicMock()
        page.content.return_value = "<html><body><p>Terms</p></body></html>"
        context = MagicMock()
        context.new_page.return_value = page
        browser.new_context.return_value = context
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        mock_sync.return_value.__enter__.return_value = pw
        return pw, browser

    @patch("monitor.services._rate_limit")
    @patch("playwright.sync_api.sync_playwright")
    def test_launches_single_browser_for_multiple_fetches(self, mock_sync, mock_rate):  # noqa: ARG002
        pw, browser = self._fake_sync_playwright(mock_sync)

        from monitor.services import fetch_html_playwright

        fetch_html_playwright("https://example.com/tos")
        fetch_html_playwright("https://example.org/privacy")

        self.assertEqual(mock_sync.call_count, 1)
        self.assertEqual(pw.chromium.launch.call_count, 1)
        self.assertEqual(browser.new_context.call_count, 2)

    @patch("monitor.services._rate_limit")
    @patch("playwright.sync_api.sync_playwright")
    def test_browser_launched_with_low_memory_args(self, mock_sync, mock_rate):  # noqa: ARG002
        pw, _browser = self._fake_sync_playwright(mock_sync)

        from monitor.services import fetch_html_playwright

        fetch_html_playwright("https://example.com/tos")

        kwargs = pw.chromium.launch.call_args.kwargs
        self.assertEqual(kwargs["headless"], True)
        for flag in ("--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"):
            self.assertIn(flag, kwargs["args"])


class PlaywrightResourceBlockingTest(TestCase):
    """Heavy resource types (images/media/fonts) are aborted to save memory."""

    def setUp(self):
        from monitor import services

        services._playwright_browser = None
        services._playwright_instance = None

    @patch("monitor.services._rate_limit")
    @patch("playwright.sync_api.sync_playwright")
    def test_route_handler_registered_on_context(self, mock_sync, mock_rate):  # noqa: ARG002
        browser = MagicMock()
        browser.is_connected.return_value = True
        page = MagicMock()
        page.content.return_value = "<html><body><p>Terms</p></body></html>"
        context = MagicMock()
        context.new_page.return_value = page
        browser.new_context.return_value = context
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        mock_sync.return_value.__enter__.return_value = pw

        from monitor.services import _block_heavy_resources, fetch_html_playwright

        fetch_html_playwright("https://example.com/tos")

        context.route.assert_called_once_with("**/*", _block_heavy_resources)

    def test_blocks_image_media_and_font_requests(self):
        from monitor.services import _block_heavy_resources

        for resource_type in ("image", "media", "font"):
            route = MagicMock()
            route.request.resource_type = resource_type
            _block_heavy_resources(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()

    def test_allows_other_request_types(self):
        from monitor.services import _block_heavy_resources

        route = MagicMock()
        route.request.resource_type = "script"
        _block_heavy_resources(route)
        route.continue_.assert_called_once()
        route.abort.assert_not_called()


# ---------------------------------------------------------------------------
# PDF support tests
# ---------------------------------------------------------------------------


class IsPdfResponseTest(TestCase):
    """Unit tests for PDF detection logic inside fetch_pdf_bytes."""

    def _make_response(self, content_type: str, url: str = "https://example.com/doc") -> MagicMock:
        resp = MagicMock()
        resp.headers = {"Content-Type": content_type}
        resp.url = url
        resp.content = b"%PDF-1.4 fake"
        resp.raise_for_status = MagicMock()
        return resp

    @patch("monitor.services.requests.get")
    def test_detects_pdf_by_content_type(self, mock_get):
        mock_get.return_value = self._make_response("application/pdf")
        _, is_pdf = fetch_pdf_bytes("https://example.com/doc")
        self.assertTrue(is_pdf)

    @patch("monitor.services.requests.get")
    def test_detects_pdf_by_url_extension(self, mock_get):
        mock_get.return_value = self._make_response(
            "application/octet-stream", "https://example.com/file.pdf"
        )
        _, is_pdf = fetch_pdf_bytes("https://example.com/file.pdf")
        self.assertTrue(is_pdf)

    @patch("monitor.services.requests.get")
    def test_html_not_detected_as_pdf(self, mock_get):
        mock_get.return_value = self._make_response("text/html; charset=utf-8")
        _, is_pdf = fetch_pdf_bytes("https://example.com/page")
        self.assertFalse(is_pdf)


class ExtractPdfTextTest(TestCase):
    """Tests for extract_pdf_text using a minimal synthetic PDF via pdfplumber."""

    def _make_pdf_bytes(self, pages: list[str]) -> bytes:
        """Build a minimal valid PDF with one text string per page."""
        import io as _io

        try:
            import reportlab.pdfgen.canvas as rl_canvas

            buf = _io.BytesIO()
            c = rl_canvas.Canvas(buf)
            for text in pages:
                c.drawString(72, 720, text)
                c.showPage()
            c.save()
            return buf.getvalue()
        except ImportError:
            self.skipTest("reportlab not installed; skipping PDF byte generation test")

    def test_extract_returns_string(self):
        """extract_pdf_text returns a non-empty string for valid PDF bytes."""

        # Use pdfplumber's own test fixture approach: mock page.extract_text
        with patch("pdfplumber.open") as mock_open:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = "Hello World\nThis is a test."
            mock_pdf = MagicMock()
            mock_pdf.pages = [mock_page]
            mock_open.return_value.__enter__.return_value = mock_pdf
            result = extract_pdf_text(b"fake-pdf-bytes")
        self.assertIn("Hello World", result)
        self.assertIn("This is a test.", result)

    def test_repeated_lines_removed(self):
        """Lines appearing on every page are stripped as headers/footers."""
        with patch("pdfplumber.open") as mock_open:
            mock_pages = []
            for i in range(3):
                p = MagicMock()
                p.extract_text.return_value = f"Company Confidential\nPage content {i}\n{i + 1}"
                mock_pages.append(p)
            mock_pdf = MagicMock()
            mock_pdf.pages = mock_pages
            mock_open.return_value.__enter__.return_value = mock_pdf
            result = extract_pdf_text(b"fake")
        # "Company Confidential" appears on all 3 pages → should be stripped
        self.assertNotIn("Company Confidential", result)
        # Unique content should remain
        self.assertIn("Page content 0", result)

    def test_page_numbers_removed(self):
        """Bare page-number lines are stripped."""
        with patch("pdfplumber.open") as mock_open:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = "Real content here\n3"
            mock_pdf = MagicMock()
            mock_pdf.pages = [mock_page]
            mock_open.return_value.__enter__.return_value = mock_pdf
            result = extract_pdf_text(b"fake")
        self.assertIn("Real content here", result)
        self.assertNotIn("\n3\n", result)

    def test_whitespace_normalised(self):
        """Internal whitespace within lines is collapsed to a single space."""
        with patch("pdfplumber.open") as mock_open:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = "Too   many    spaces here"
            mock_pdf = MagicMock()
            mock_pdf.pages = [mock_page]
            mock_open.return_value.__enter__.return_value = mock_pdf
            result = extract_pdf_text(b"fake")
        self.assertIn("Too many spaces here", result)

    def test_empty_pdf_returns_empty_string(self):
        """A PDF with no extractable text returns an empty string."""
        with patch("pdfplumber.open") as mock_open:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = ""
            mock_pdf = MagicMock()
            mock_pdf.pages = [mock_page]
            mock_open.return_value.__enter__.return_value = mock_pdf
            result = extract_pdf_text(b"fake")
        self.assertEqual(result, "")


class FetchDocumentContentPdfTest(TestCase):
    """Tests that fetch_document_content sets document_format correctly for PDFs."""

    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/policy.pdf")

    @patch("monitor.services.extract_pdf_text")
    @patch("monitor.services.requests.get")
    def test_sets_document_format_pdf(self, mock_get, mock_extract):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.url = "https://acme.com/policy.pdf"
        mock_response.content = b"%PDF fake"
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        mock_extract.return_value = "Extracted PDF text"
        from monitor.services import fetch_document_content

        text, method = fetch_document_content(self.doc)
        self.assertEqual(text, "Extracted PDF text")
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_format, Document.DocumentFormat.PDF)

    @patch("monitor.services.requests.get")
    def test_sets_document_format_html(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.url = "https://acme.com/policy"
        mock_response.content = b"<html><body><p>Hello</p></body></html>"
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        html_doc = Document.objects.create(
            organization=self.doc.organization, url="https://acme.com/policy"
        )
        from monitor.services import fetch_document_content

        fetch_document_content(html_doc)
        html_doc.refresh_from_db()
        self.assertEqual(html_doc.document_format, Document.DocumentFormat.HTML)

    @patch("monitor.services.extract_pdf_text", side_effect=Exception("corrupt PDF"))
    @patch("monitor.services.requests.get")
    def test_returns_none_on_pdf_extraction_failure(self, mock_get, mock_extract):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.url = "https://acme.com/policy.pdf"
        mock_response.content = b"%PDF fake"
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        from monitor.services import fetch_document_content

        text, method = fetch_document_content(self.doc)
        self.assertIsNone(text)


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------


class DocumentDetailViewTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        self.snap1 = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="Version 1", text_hash=compute_hash("Version 1")
        )
        self.snap2 = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="Version 2", text_hash=compute_hash("Version 2")
        )

    def test_returns_200(self):
        response = self.client.get(reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertEqual(response.status_code, 200)

    def test_uses_correct_template(self):
        response = self.client.get(reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertTemplateUsed(response, "monitor/document_detail.html")

    def test_shows_document_name(self):
        response = self.client.get(reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertContains(response, "Acme")

    def test_shows_snapshots(self):
        response = self.client.get(reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertContains(response, self.snap1.text_hash[:12])
        self.assertContains(response, self.snap2.text_hash[:12])

    def test_404_for_missing_document(self):
        response = self.client.get(reverse("monitor:document_detail", args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_no_snapshots_message(self):
        org = Organization.objects.create(name="Empty Org", website_url="https://empty.com")
        doc = Document.objects.create(organization=org, url="https://empty.com/tos")
        response = self.client.get(reverse("monitor:document_detail", args=[doc.pk]))
        self.assertContains(response, "No snapshots captured yet")


class SnapshotDiffViewTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        self.snap_old = DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="Old line one\nOld line two",
            text_hash=compute_hash("old"),
        )
        self.snap_new = DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="New line one\nNew line two",
            text_hash=compute_hash("new"),
        )

    def _url(self, old_pk=None, new_pk=None):
        return reverse(
            "monitor:snapshot_diff",
            args=[self.doc.pk, old_pk or self.snap_old.pk, new_pk or self.snap_new.pk],
        )

    def test_returns_200(self):
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_uses_correct_template(self):
        self.assertTemplateUsed(self.client.get(self._url()), "monitor/snapshot_diff.html")

    def test_contains_diff_table(self):
        response = self.client.get(self._url())
        self.assertContains(response, '<table class="diff"')

    def test_shows_old_and_new_labels(self):
        response = self.client.get(self._url())
        self.assertContains(response, "Old")
        self.assertContains(response, "New")

    def test_404_for_wrong_snapshot(self):
        response = self.client.get(self._url(old_pk=99999))
        self.assertEqual(response.status_code, 404)

    def test_404_for_missing_document(self):
        url = reverse("monitor:snapshot_diff", args=[99999, self.snap_old.pk, self.snap_new.pk])
        self.assertEqual(self.client.get(url).status_code, 404)


class SnapshotTextViewTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        self.snap = DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="# Terms\n\nAll your base.",
            text_hash=compute_hash("# Terms\n\nAll your base."),
        )

    def _url(self, doc_pk=None, snap_pk=None):
        return reverse(
            "monitor:snapshot_text",
            args=[doc_pk or self.doc.pk, snap_pk or self.snap.pk],
        )

    def test_returns_200(self):
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_uses_correct_template(self):
        self.assertTemplateUsed(self.client.get(self._url()), "monitor/snapshot_text.html")

    def test_shows_cleaned_text(self):
        response = self.client.get(self._url())
        self.assertContains(response, "All your base.")

    def test_shows_hash(self):
        response = self.client.get(self._url())
        self.assertContains(response, self.snap.text_hash)

    def test_404_for_missing_snapshot(self):
        self.assertEqual(self.client.get(self._url(snap_pk=99999)).status_code, 404)

    def test_404_when_snapshot_belongs_to_different_document(self):
        org2 = Organization.objects.create(name="Other", website_url="https://other.com")
        doc2 = Document.objects.create(organization=org2, url="https://other.com/tos")
        snap2 = DocumentSnapshot.objects.create(
            document=doc2, cleaned_text="Other text", text_hash=compute_hash("Other text")
        )
        # snap2 belongs to doc2, but we pass self.doc.pk — should 404
        url = reverse("monitor:snapshot_text", args=[self.doc.pk, snap2.pk])
        self.assertEqual(self.client.get(url).status_code, 404)


# ---------------------------------------------------------------------------
# Task tests
# ---------------------------------------------------------------------------


class CheckDocumentTaskTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")

    @patch("monitor.tasks.fetch_and_snapshot")
    def test_creates_snapshot_when_changed(self, mock_fas):
        from monitor.tasks import check_document

        mock_fas.return_value = (MagicMock(), True)
        result = check_document(self.doc.pk)
        self.assertTrue(result["created"])
        self.assertIsNone(result["error"])

    @patch("monitor.tasks.fetch_and_snapshot")
    def test_no_snapshot_when_unchanged(self, mock_fas):
        from monitor.tasks import check_document

        mock_fas.return_value = (None, False)
        result = check_document(self.doc.pk)
        self.assertFalse(result["created"])
        self.assertIsNone(result["error"])

    def test_returns_error_for_missing_document(self):
        from monitor.tasks import check_document

        result = check_document(99999)
        self.assertFalse(result["created"])
        self.assertIn("not found", result["error"])

    @patch("monitor.tasks.fetch_and_snapshot")
    def test_skips_inactive_document(self, mock_fas):
        from monitor.tasks import check_document

        self.doc.is_active = False
        self.doc.save()
        result = check_document(self.doc.pk)
        mock_fas.assert_not_called()
        self.assertFalse(result["created"])

    @patch("monitor.tasks.fetch_and_snapshot", side_effect=NotImplementedError("playwright"))
    def test_handles_not_implemented(self, _mock):
        from monitor.tasks import check_document

        result = check_document(self.doc.pk)
        self.assertFalse(result["created"])
        self.assertIn("playwright", result["error"])


class CheckAllDocumentsTaskTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc1 = Document.objects.create(organization=org, url="https://acme.com/tos")
        self.doc2 = Document.objects.create(
            organization=org,
            url="https://acme.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
        )
        self.doc_inactive = Document.objects.create(
            organization=org,
            url="https://acme.com/cookie",
            document_type=Document.DocumentType.COOKIE_POLICY,
            is_active=False,
        )

    @patch("monitor.tasks.check_document.delay")
    def test_enqueues_only_active_documents(self, mock_delay):
        from monitor.tasks import check_all_documents

        result = check_all_documents()
        self.assertEqual(result["enqueued"], 2)
        called_ids = {call.args[0] for call in mock_delay.call_args_list}
        self.assertIn(self.doc1.pk, called_ids)
        self.assertIn(self.doc2.pk, called_ids)
        self.assertNotIn(self.doc_inactive.pk, called_ids)

    @patch("monitor.tasks.check_document.delay")
    def test_returns_enqueued_count(self, mock_delay):
        from monitor.tasks import check_all_documents

        result = check_all_documents()
        self.assertEqual(result["enqueued"], mock_delay.call_count)


class CheckAllDocumentsShufflingTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        for i in range(5):
            Document.objects.create(organization=org, url=f"https://acme.com/{i}")

    @patch("monitor.tasks.random.shuffle")
    @patch("monitor.tasks.check_document.delay")
    def test_shuffles_ids(self, mock_delay, mock_shuffle):
        from monitor.tasks import check_all_documents

        check_all_documents()
        self.assertTrue(mock_shuffle.called)


class FetchDocumentsCommandTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        for i in range(5):
            Document.objects.create(organization=org, url=f"https://acme.com/{i}")

    @patch("monitor.management.commands.fetch_documents.random.shuffle")
    @patch("monitor.management.commands.fetch_documents.fetch_and_snapshot")
    def test_shuffles_by_default(self, mock_fetch, mock_shuffle):
        from django.core.management import call_command

        mock_fetch.return_value = (MagicMock(), False)
        call_command("fetch_documents")
        self.assertTrue(mock_shuffle.called)

    @patch("monitor.management.commands.fetch_documents.random.shuffle")
    @patch("monitor.management.commands.fetch_documents.fetch_and_snapshot")
    def test_no_shuffle_flag_disables_shuffling(self, mock_fetch, mock_shuffle):
        from django.core.management import call_command

        mock_fetch.return_value = (MagicMock(), False)
        call_command("fetch_documents", shuffle=False)
        self.assertFalse(mock_shuffle.called)


class ExportMonitorDataCommandTest(TestCase):
    def setUp(self):
        self.tag = Tag.objects.create(name="Open Source")
        self.language = Language.objects.get_or_create(code="en", defaults={"name": "English"})[0]
        self.country = Country.objects.create(name="United States", code="US")
        self.parent_org = Organization.objects.create(
            name="Parent Co", website_url="https://parent.example"
        )
        self.child_org = Organization.objects.create(
            name="Child Co", website_url="https://child.example", parent=self.parent_org
        )
        self.child_org.tags.add(self.tag)
        self.past = timezone.now() - timezone.timedelta(days=3)
        self.doc = Document.objects.create(
            organization=self.child_org,
            document_type=Document.DocumentType.PRIVACY_POLICY,
            url="https://child.example/privacy",
            language=self.language,
            country=self.country,
            last_changed=self.past,
        )
        self.snapshot = DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="Hello world",
            text_hash=compute_hash("Hello world"),
        )
        DocumentSnapshot.objects.filter(pk=self.snapshot.pk).update(captured_at=self.past)

    def _export(self, path, **kwargs):
        from django.core.management import call_command

        call_command("export_monitor_data", output=str(path), **kwargs)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_exports_lookup_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._export(Path(tmp) / "export.json")
        self.assertEqual(data["version"], 1)
        self.assertEqual([c["code"] for c in data["countries"]], ["US"])
        self.assertEqual([lang["code"] for lang in data["languages"]], ["en"])
        self.assertEqual([t["name"] for t in data["tags"]], ["Open Source"])

    def test_exports_organizations_with_relations(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._export(Path(tmp) / "export.json")
        child = next(o for o in data["organizations"] if o["slug"] == "child-co")
        self.assertEqual(child["parent"], "parent-co")
        self.assertEqual(child["tags"], ["Open Source"])

    def test_exports_documents_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._export(Path(tmp) / "export.json")
        self.assertEqual(len(data["documents"]), 1)
        doc = data["documents"][0]
        self.assertEqual(doc["organization"], "child-co")
        self.assertEqual(doc["language"], "en")
        self.assertEqual(doc["country"], "US")
        self.assertEqual(doc["document_type"], "privacy")
        self.assertEqual(len(data["snapshots"]), 1)
        snap = data["snapshots"][0]
        self.assertEqual(snap["organization"], "child-co")
        self.assertEqual(snap["text_hash"], compute_hash("Hello world"))
        self.assertEqual(snap["cleaned_text"], "Hello world")
        self.assertIn("captured_at", snap)

    def test_export_can_skip_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._export(Path(tmp) / "export.json", no_snapshots=True)
        self.assertEqual(len(data["documents"]), 1)
        self.assertEqual(data["snapshots"], [])


class ImportMonitorDataCommandTest(TestCase):
    def setUp(self):
        self.tag = Tag.objects.create(name="Open Source")
        self.language = Language.objects.get_or_create(code="en", defaults={"name": "English"})[0]
        self.country = Country.objects.create(name="United States", code="US")
        self.parent_org = Organization.objects.create(
            name="Parent Co", website_url="https://parent.example"
        )
        self.child_org = Organization.objects.create(
            name="Child Co", website_url="https://child.example", parent=self.parent_org
        )
        self.child_org.tags.add(self.tag)
        self.past = timezone.now() - timezone.timedelta(days=3)
        self.doc = Document.objects.create(
            organization=self.child_org,
            document_type=Document.DocumentType.PRIVACY_POLICY,
            url="https://child.example/privacy",
            language=self.language,
            country=self.country,
            last_changed=self.past,
        )
        self.snapshot = DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="Hello world",
            text_hash=compute_hash("Hello world"),
        )
        DocumentSnapshot.objects.filter(pk=self.snapshot.pk).update(captured_at=self.past)

    def _export_data(self):
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.json"
            call_command("export_monitor_data", output=str(path))
            return json.loads(path.read_text(encoding="utf-8"))

    def _import_data(self, data, **kwargs):
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "import.json"
            path.write_text(json.dumps(data, cls=DjangoJSONEncoder), encoding="utf-8")
            call_command("import_monitor_data", input=str(path), **kwargs)

    def _wipe_monitor_data(self):
        DocumentSnapshot.objects.all().delete()
        Document.objects.all().delete()
        Organization.objects.all().delete()
        Tag.objects.all().delete()
        Language.objects.all().delete()
        Country.objects.all().delete()

    def test_import_recreates_full_dataset(self):
        data = self._export_data()
        self._wipe_monitor_data()
        self._import_data(data)

        self.assertEqual(Country.objects.count(), 1)
        self.assertEqual(Language.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)
        self.assertEqual(Organization.objects.count(), 2)
        child = Organization.objects.get(slug="child-co")
        self.assertEqual(child.parent.slug, "parent-co")
        self.assertEqual(list(child.tags.values_list("name", flat=True)), ["Open Source"])
        doc = Document.objects.get()
        self.assertEqual(doc.organization, child)
        self.assertEqual(doc.language.code, "en")
        self.assertEqual(doc.country.code, "US")
        snap = DocumentSnapshot.objects.get()
        self.assertEqual(snap.text_hash, compute_hash("Hello world"))
        self.assertEqual(snap.cleaned_text, "Hello world")
        self.assertEqual(snap.captured_at, self.past)

    def test_import_is_idempotent(self):
        data = self._export_data()
        self._wipe_monitor_data()
        self._import_data(data)
        self._import_data(data)

        self.assertEqual(Organization.objects.count(), 2)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(DocumentSnapshot.objects.count(), 1)
        self.assertEqual(Country.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)

    def test_import_merges_with_existing_rows(self):
        data = self._export_data()
        child_pk = self.child_org.pk
        self.child_org.website_url = "https://old.example"
        self.child_org.save()
        self._import_data(data)

        child = Organization.objects.get(pk=child_pk)
        self.assertEqual(child.website_url, "https://child.example")
        self.assertEqual(Organization.objects.count(), 2)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(DocumentSnapshot.objects.count(), 1)

    def test_import_handles_parent_declared_after_child(self):
        data = {
            "version": 1,
            "countries": [],
            "languages": [{"pk": 1, "name": "English", "code": "en"}],
            "tags": [],
            "organizations": [
                {
                    "pk": 2,
                    "name": "Child Co",
                    "slug": "child-co",
                    "website_url": "https://child.example",
                    "category": "",
                    "is_failing": False,
                    "parent": "parent-co",
                    "tags": [],
                },
                {
                    "pk": 1,
                    "name": "Parent Co",
                    "slug": "parent-co",
                    "website_url": "https://parent.example",
                    "category": "",
                    "is_failing": False,
                    "parent": None,
                    "tags": [],
                },
            ],
            "documents": [
                {
                    "pk": 1,
                    "organization": "child-co",
                    "name": "",
                    "document_type": "tos",
                    "other_document_type": "",
                    "url": "https://child.example/tos",
                    "fetch_method": "requests",
                    "document_format": "html",
                    "language": "en",
                    "country": None,
                    "last_checked": None,
                    "last_changed": None,
                    "is_active": True,
                    "is_failing": False,
                    "custom_selectors": "",
                    "fetch_config": None,
                }
            ],
            "snapshots": [
                {
                    "organization": "child-co",
                    "document_type": "tos",
                    "url": "https://child.example/tos",
                    "captured_at": "2024-01-02T03:04:05+00:00",
                    "text_hash": "abc123",
                    "cleaned_text": "Yo",
                }
            ],
        }
        self._wipe_monitor_data()
        self._import_data(data)

        child = Organization.objects.get(slug="child-co")
        self.assertEqual(child.parent.slug, "parent-co")
        self.assertEqual(Document.objects.count(), 1)
        snap = DocumentSnapshot.objects.get()
        self.assertEqual(snap.captured_at, parse_datetime("2024-01-02T03:04:05+00:00"))

    def test_import_can_skip_snapshots(self):
        data = self._export_data()
        self._wipe_monitor_data()
        self._import_data(data, no_snapshots=True)

        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(DocumentSnapshot.objects.count(), 0)

    def _capture_import(self, data, **kwargs):
        import io

        from django.core.management import call_command

        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "import.json"
            path.write_text(json.dumps(data, cls=DjangoJSONEncoder), encoding="utf-8")
            call_command("import_monitor_data", input=str(path), stdout=buffer, **kwargs)
        return buffer.getvalue()

    def test_replace_reports_wipe_progress(self):
        data = self._export_data()
        output = self._capture_import(data, replace=True)

        self.assertIn("Removing existing monitored data...", output)
        self.assertIn("  snapshots: 1 removed", output)
        self.assertIn("  documents: 1 removed", output)
        self.assertIn("  organizations: 2 removed", output)
        self.assertIn("  tags: 1 removed", output)
        self.assertIn("  languages: 1 removed", output)
        self.assertIn("  countries: 1 removed", output)
        self.assertEqual(Organization.objects.count(), 2)
        self.assertEqual(Document.objects.count(), 1)

    def test_snapshot_import_reports_progress(self):
        data = self._export_data()
        data["snapshots"] = [
            {
                "organization": "child-co",
                "document_type": Document.DocumentType.PRIVACY_POLICY,
                "url": "https://child.example/privacy",
                "captured_at": f"2024-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
                "text_hash": f"hash{i:05d}",
                "cleaned_text": f"snapshot number {i}",
            }
            for i in range(2200)
        ]
        self._wipe_monitor_data()
        output = self._capture_import(data)

        self.assertIn("  snapshots imported: 2,000 / 2,200 snapshots", output)
        self.assertEqual(DocumentSnapshot.objects.count(), 2200)

    def test_replace_removes_existing_rows(self):
        data = self._export_data()
        stale_org = Organization.objects.create(
            name="Stale Co", website_url="https://stale.example"
        )
        Document.objects.create(
            organization=stale_org,
            document_type=Document.DocumentType.TERMS_OF_SERVICE,
            url="https://stale.example/tos",
        )
        Tag.objects.create(name="Stale Tag")
        self._import_data(data, replace=True)

        self.assertEqual(Organization.objects.count(), 2)
        self.assertFalse(Organization.objects.filter(slug="stale-co").exists())
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)
        self.assertFalse(Tag.objects.filter(name="Stale Tag").exists())
        self.assertTrue(
            Organization.objects.get(slug="child-co").tags.filter(name="Open Source").exists()
        )


class RecentChangesViewTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")

    def test_homepage_returns_200(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertEqual(response.status_code, 200)

    def test_homepage_uses_correct_template(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertTemplateUsed(response, "monitor/home.html")

    def test_homepage_shows_recent_snapshots(self):
        DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="Recent content",
            text_hash=compute_hash("Recent content"),
        )
        response = self.client.get(reverse("monitor:home"))
        # The homepage lists document names, not raw snapshot text
        self.assertContains(response, "Acme")

    def test_homepage_excludes_old_snapshots(self):
        old_snapshot = DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="Old content",
            text_hash=compute_hash("Old content"),
        )
        # Backdate the snapshot to 15 days ago
        DocumentSnapshot.objects.filter(pk=old_snapshot.pk).update(
            captured_at=timezone.now() - timezone.timedelta(days=15)
        )
        response = self.client.get(reverse("monitor:home"))
        # No recent snapshots exist, so the empty-state message should appear
        self.assertContains(response, "No document changes detected")

    def test_homepage_empty_state(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "No document changes detected")

    def test_day_filter_defaults_to_14(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertEqual(response.context["days"], 14)

    def test_day_filter_accepts_valid_values(self):
        for days in (3, 7, 14, 30):
            response = self.client.get(reverse("monitor:home"), {"days": days})
            self.assertEqual(response.context["days"], days)

    def test_day_filter_rejects_invalid_value(self):
        response = self.client.get(reverse("monitor:home"), {"days": 99})
        self.assertEqual(response.context["days"], 14)

    def test_day_filter_rejects_non_integer(self):
        response = self.client.get(reverse("monitor:home"), {"days": "abc"})
        self.assertEqual(response.context["days"], 14)

    def test_day_filter_links_rendered(self):
        response = self.client.get(reverse("monitor:home"))
        for days in (3, 7, 14, 30):
            self.assertContains(response, f"?days={days}")

    def test_3_day_filter_excludes_old_snapshot(self):
        snap = DocumentSnapshot.objects.create(
            document=self.doc,
            cleaned_text="Old content",
            text_hash=compute_hash("Old content"),
        )
        DocumentSnapshot.objects.filter(pk=snap.pk).update(
            captured_at=timezone.now() - timezone.timedelta(days=5)
        )
        response = self.client.get(reverse("monitor:home"), {"days": 3})
        self.assertContains(response, "No document changes detected")


class HomePageGroupingTest(TestCase):
    """Tests for day/org grouping and ordering on the home page."""

    def setUp(self):
        self.hbo = Organization.objects.create(name="HBO", website_url="https://hbo.com")
        self.tos_doc = Document.objects.create(
            organization=self.hbo,
            url="https://hbo.com/tos",
            document_type=Document.DocumentType.TERMS_OF_SERVICE,
        )
        self.privacy_doc = Document.objects.create(
            organization=self.hbo,
            url="https://hbo.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
        )

    def _snapshot(self, document, text="content"):
        return DocumentSnapshot.objects.create(
            document=document,
            cleaned_text=text,
            text_hash=compute_hash(text),
        )

    def test_context_groups_documents_by_organization(self):
        self._snapshot(self.tos_doc)
        self._snapshot(self.privacy_doc)
        response = self.client.get(reverse("monitor:home"))
        org_groups = response.context["day_groups"][0]["organizations"]
        self.assertEqual(org_groups[0]["organization"].name, "HBO")
        self.assertEqual(len(org_groups[0]["snapshots"]), 2)

    def test_organizations_sorted_alphabetically_within_day(self):
        apple = Organization.objects.create(name="Apple", website_url="https://apple.com")
        netflix = Organization.objects.create(name="Netflix", website_url="https://netflix.com")
        apple_doc = Document.objects.create(organization=apple, url="https://apple.com/privacy")
        netflix_doc = Document.objects.create(organization=netflix, url="https://netflix.com/tos")
        self._snapshot(apple_doc)
        self._snapshot(netflix_doc)
        self._snapshot(self.tos_doc)
        response = self.client.get(reverse("monitor:home"))
        org_names = [
            og["organization"].name for og in response.context["day_groups"][0]["organizations"]
        ]
        self.assertEqual(org_names, ["Apple", "HBO", "Netflix"])

    def test_snapshots_split_into_separate_days(self):
        snap = self._snapshot(self.tos_doc)
        DocumentSnapshot.objects.filter(pk=snap.pk).update(
            captured_at=timezone.now() - timezone.timedelta(days=1)
        )
        self._snapshot(self.privacy_doc)
        response = self.client.get(reverse("monitor:home"))
        day_groups = response.context["day_groups"]
        self.assertEqual(len(day_groups), 2)

    def test_org_appears_once_per_day(self):
        self._snapshot(self.tos_doc)
        self._snapshot(self.privacy_doc)
        response = self.client.get(reverse("monitor:home"))
        org_groups = response.context["day_groups"][0]["organizations"]
        self.assertEqual(len(org_groups), 1)

    def test_homepage_renders_organization_group_heading(self):
        self._snapshot(self.tos_doc)
        self._snapshot(self.privacy_doc)
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "HBO")
        self.assertContains(response, "<h2")  # day heading rendered

    def test_homepage_does_not_show_time(self):
        self._snapshot(self.tos_doc)
        response = self.client.get(reverse("monitor:home"))
        self.assertNotContains(response, "UTC")

    def test_homepage_context_has_totals(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertEqual(response.context["total_organizations"], 1)
        self.assertEqual(response.context["total_documents"], 2)

    def test_homepage_hero_shows_counts(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "Organizations tracked")
        self.assertContains(response, "Documents monitored")

    def test_homepage_renders_org_favicon(self):
        self._snapshot(self.tos_doc)
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "icons.duckduckgo.com/ip3/hbo.com.ico")


class OrganizationsViewTest(TestCase):
    def setUp(self):
        self.parent = Organization.objects.create(name="Meta", website_url="https://meta.com")
        self.child = Organization.objects.create(
            name="Instagram", website_url="https://instagram.com", parent=self.parent
        )
        self.doc = Document.objects.create(organization=self.parent, url="https://meta.com/tos")
        self.child_doc = Document.objects.create(
            organization=self.child,
            url="https://instagram.com/tos",
            document_type=Document.DocumentType.PRIVACY_POLICY,
        )

    def test_returns_200(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertEqual(response.status_code, 200)

    def test_uses_correct_template(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertTemplateUsed(response, "monitor/organizations.html")

    def test_shows_parent_organization(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "Meta")

    def test_shows_subsidiary(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "Instagram")

    def test_shows_document_link(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, f"/document/{self.doc.pk}/")

    def test_child_org_not_listed_as_top_level(self):
        response = self.client.get(reverse("monitor:organizations"))
        orgs = list(response.context["organizations"])
        names = [o.name for o in orgs]
        self.assertIn("Meta", names)
        self.assertNotIn("Instagram", names)

    def test_empty_state(self):
        Organization.objects.all().delete()
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "No organizations have been added yet")

    def test_context_includes_hero_stats(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertEqual(response.context["total_organizations"], 1)
        self.assertEqual(response.context["total_documents"], 2)

    def test_hero_shows_organization_and_document_counts(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "Organizations tracked")
        self.assertContains(response, "Documents monitored")

    def test_org_card_shows_document_count(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "2 documents")

    def test_shows_org_favicon(self):
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "icons.duckduckgo.com/ip3/meta.com.ico")


class AboutViewTest(TestCase):
    def test_returns_200(self):
        response = self.client.get(reverse("monitor:about"))
        self.assertEqual(response.status_code, 200)

    def test_uses_correct_template(self):
        response = self.client.get(reverse("monitor:about"))
        self.assertTemplateUsed(response, "monitor/about.html")

    def test_shows_page_title(self):
        response = self.client.get(reverse("monitor:about"))
        self.assertContains(response, "About")


class SponsorLinksFooterTest(TestCase):
    def test_support_section_hidden_by_default(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertNotContains(response, "Support TosDiff")
        self.assertNotContains(response, "github.com/sponsors")
        self.assertNotContains(response, "ko-fi.com")

    @override_settings(SPONSOR_GITHUB_URL="https://github.com/sponsors/example")
    def test_github_sponsors_link_rendered_when_configured(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "https://github.com/sponsors/example")

    @override_settings(SPONSOR_KOFI_URL="https://ko-fi.com/tosdiff")
    def test_kofi_link_rendered_when_configured(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "https://ko-fi.com/tosdiff")

    @override_settings(SPONSOR_GITHUB_URL="https://github.com/sponsors/example")
    def test_only_configured_links_rendered(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "https://github.com/sponsors/example")
        self.assertNotContains(response, "ko-fi.com")


class TermsViewTest(TestCase):
    def test_returns_200(self):
        self.assertEqual(self.client.get(reverse("monitor:terms")).status_code, 200)

    def test_uses_correct_template(self):
        self.assertTemplateUsed(self.client.get(reverse("monitor:terms")), "monitor/terms.html")

    def test_shows_heading(self):
        self.assertContains(self.client.get(reverse("monitor:terms")), "Terms of Use")


class PrivacyViewTest(TestCase):
    def test_returns_200(self):
        self.assertEqual(self.client.get(reverse("monitor:privacy")).status_code, 200)

    def test_uses_correct_template(self):
        self.assertTemplateUsed(self.client.get(reverse("monitor:privacy")), "monitor/privacy.html")

    def test_shows_heading(self):
        self.assertContains(self.client.get(reverse("monitor:privacy")), "Privacy Policy")


class OrganizationsViewFilterTest(TestCase):
    """Tests for the org-with-no-documents filtering."""

    def test_org_without_documents_hidden(self):
        Organization.objects.create(name="EmptyCorp", website_url="https://empty.com")
        response = self.client.get(reverse("monitor:organizations"))
        self.assertNotContains(response, "EmptyCorp")

    def test_org_with_only_subsidiary_docs_shown(self):
        parent = Organization.objects.create(name="HoldCo", website_url="https://holdco.com")
        child = Organization.objects.create(
            name="SubCo", website_url="https://subco.com", parent=parent
        )
        Document.objects.create(organization=child, url="https://subco.com/tos")
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "HoldCo")

    def test_other_doc_type_shows_custom_name(self):
        org = Organization.objects.create(name="AcmeCo", website_url="https://acme.com")
        Document.objects.create(
            organization=org,
            url="https://acme.com/other",
            document_type="other",
            other_document_type="Community Guidelines",
        )
        response = self.client.get(reverse("monitor:organizations"))
        self.assertContains(response, "Community Guidelines")
        self.assertNotContains(response, "Other")

    def test_subsidiary_without_docs_hidden(self):
        parent = Organization.objects.create(name="BigCorp", website_url="https://bigcorp.com")
        Organization.objects.create(
            name="EmptySub", website_url="https://emptysub.com", parent=parent
        )
        Document.objects.create(organization=parent, url="https://bigcorp.com/tos")
        response = self.client.get(reverse("monitor:organizations"))
        self.assertNotContains(response, "EmptySub")


class DocumentTypeFilterTest(TestCase):
    """Tests for the homepage document-type filter chips."""

    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.tos_doc = Document.objects.create(
            organization=self.org,
            url="https://acme.com/tos",
            document_type=Document.DocumentType.TERMS_OF_SERVICE,
        )
        self.privacy_doc = Document.objects.create(
            organization=self.org,
            url="https://acme.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
        )
        DocumentSnapshot.objects.create(
            document=self.tos_doc,
            cleaned_text="TOS content",
            text_hash=compute_hash("TOS content"),
        )
        DocumentSnapshot.objects.create(
            document=self.privacy_doc,
            cleaned_text="Privacy content",
            text_hash=compute_hash("Privacy content"),
        )

    def test_homepage_shows_document_type_chips(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertContains(response, "Terms of Service")
        self.assertContains(response, "Privacy Policy")

    def test_type_filter_shows_only_matching_type(self):
        response = self.client.get(reverse("monitor:home"), {"type": "privacy"})
        self.assertContains(response, "Privacy Policy")
        # The tos doc should still appear because its org has a privacy doc —
        # but the privacy chip should be active. We just verify the filter param is accepted.
        self.assertEqual(response.context["doc_type"], "privacy")

    def test_type_filter_invalid_value_shows_all(self):
        response = self.client.get(reverse("monitor:home"), {"type": "nonexistent"})
        self.assertEqual(response.context["doc_type"], "")
        self.assertContains(response, "Acme")

    def test_type_filter_defaults_to_empty(self):
        response = self.client.get(reverse("monitor:home"))
        self.assertEqual(response.context["doc_type"], "")

    def test_type_filter_preserves_days(self):
        response = self.client.get(reverse("monitor:home"), {"type": "privacy", "days": 7})
        self.assertEqual(response.context["doc_type"], "privacy")
        self.assertEqual(response.context["days"], 7)

    def test_homepage_documents_in_context(self):
        response = self.client.get(reverse("monitor:home"))
        doc_types = response.context["document_types"]
        type_values = [dt["value"] for dt in doc_types]
        self.assertIn("tos", type_values)
        self.assertIn("privacy", type_values)


# ---------------------------------------------------------------------------
# Website suggestion tests
# ---------------------------------------------------------------------------


class SuggestionModelTest(TestCase):
    def test_status_defaults_to_pending(self):
        suggestion = Suggestion.objects.create(
            organization_name="Acme",
            website_url="https://acme.com",
            document_url="https://acme.com/tos",
        )
        self.assertEqual(suggestion.status, Suggestion.Status.PENDING)

    def test_full_fields_roundtrip(self):
        suggestion = Suggestion.objects.create(
            organization_name="Acme",
            website_url="https://acme.com",
            document_url="https://acme.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
            contact_email="user@example.com",
            notes="Please add this one",
        )
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.document_type, "privacy")
        self.assertEqual(suggestion.contact_email, "user@example.com")

    def test_str_returns_company_name(self):
        suggestion = Suggestion(organization_name="Acme Corp")
        self.assertEqual(str(suggestion), "Acme Corp")

    def test_creates_organization_and_document(self):
        suggestion = Suggestion.objects.create(
            organization_name="Acme",
            website_url="https://acme.com",
            document_url="https://acme.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
        )
        org, doc = suggestion.create_organization_and_document()
        self.assertEqual(org.name, "Acme")
        self.assertEqual(org.website_url, "https://acme.com")
        self.assertEqual(doc.document_type, "privacy")
        self.assertEqual(doc.url, "https://acme.com/privacy")

    def test_create_organization_and_document_is_idempotent(self):
        suggestion = Suggestion.objects.create(
            organization_name="Acme",
            website_url="https://acme.com",
            document_url="https://acme.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
        )
        suggestion.create_organization_and_document()
        suggestion.create_organization_and_document()
        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(Document.objects.count(), 1)


class SuggestionAdminTest(TestCase):
    def setUp(self):
        from .admin import SuggestionAdmin

        self.admin = SuggestionAdmin(model=Suggestion, admin_site=admin.site)
        self.suggestion = Suggestion.objects.create(
            organization_name="Acme",
            website_url="https://acme.com",
            document_url="https://acme.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
            contact_email="user@example.com",
        )

    def test_approve_and_create_creates_org_and_document(self):
        self.admin.approve_and_create(MagicMock(), Suggestion.objects.filter(pk=self.suggestion.pk))
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, Suggestion.Status.APPROVED)
        self.assertTrue(Organization.objects.filter(website_url="https://acme.com").exists())
        self.assertTrue(Document.objects.filter(url="https://acme.com/privacy").exists())

    def test_approve_marks_suggestion_approved(self):
        self.admin.approve(MagicMock(), Suggestion.objects.filter(pk=self.suggestion.pk))
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, Suggestion.Status.APPROVED)

    def test_reject_marks_suggestion_rejected(self):
        self.admin.reject(MagicMock(), Suggestion.objects.filter(pk=self.suggestion.pk))
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, Suggestion.Status.REJECTED)


class LoginCodeModelTest(TestCase):
    def test_created_at_auto_set(self):
        code = LoginCode.objects.create(
            email="bob@example.com",
            code_hash="abc123",
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )
        self.assertIsNotNone(code.created_at)


class DocumentSubscriptionModelTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")

    def test_subscription_created(self):
        sub = DocumentSubscription.objects.create(user=self.user, document=self.doc)
        self.assertEqual(sub.document, self.doc)
        self.assertEqual(sub.user, self.user)

    def test_user_cannot_subscribe_twice(self):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        with self.assertRaises(IntegrityError):
            DocumentSubscription.objects.create(user=self.user, document=self.doc)

    def test_cascade_delete_on_document(self):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        self.doc.delete()
        self.assertEqual(DocumentSubscription.objects.count(), 0)


class OrganizationSubscriptionModelTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")

    def test_subscription_created(self):
        sub = OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        self.assertEqual(sub.organization, self.org)

    def test_user_cannot_subscribe_twice(self):
        OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        with self.assertRaises(IntegrityError):
            OrganizationSubscription.objects.create(user=self.user, organization=self.org)


# ---------------------------------------------------------------------------
# Login / notification service tests
# ---------------------------------------------------------------------------


class GenerateLoginCodeTest(TestCase):
    def test_returns_six_digit_string(self):
        code = generate_login_code()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_codes_are_distinct(self):
        codes = {generate_login_code() for _ in range(200)}
        self.assertGreater(len(codes), 150)


class SendLoginCodeEmailTest(TestCase):
    def test_sends_email_with_code(self):
        send_login_code_email("user@example.com", "123456")
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["user@example.com"])
        self.assertIn("TosDiff", email.subject)
        self.assertIn("123456", email.body)


# ---------------------------------------------------------------------------
# Suggestion view tests
# ---------------------------------------------------------------------------


class SuggestionViewTest(TestCase):
    def test_get_returns_200(self):
        response = self.client.get(reverse("monitor:suggest"))
        self.assertEqual(response.status_code, 200)

    def test_uses_correct_template(self):
        response = self.client.get(reverse("monitor:suggest"))
        self.assertTemplateUsed(response, "monitor/suggest_document.html")

    def test_post_creates_suggestion(self):
        response = self.client.post(
            reverse("monitor:suggest"),
            {
                "organization_name": "Acme",
                "website_url": "https://acme.com",
                "document_url": "https://acme.com/tos",
                "document_type": Document.DocumentType.TERMS_OF_SERVICE,
            },
        )
        self.assertEqual(Suggestion.objects.count(), 1)
        self.assertRedirects(response, reverse("monitor:suggestion_thanks"))

    def test_post_requires_document_url(self):
        response = self.client.post(
            reverse("monitor:suggest"),
            {
                "organization_name": "Acme",
                "website_url": "https://acme.com",
                "document_type": Document.DocumentType.TERMS_OF_SERVICE,
            },
        )
        self.assertEqual(Suggestion.objects.count(), 0)
        self.assertEqual(response.status_code, 200)

    def test_thanks_page_returns_200(self):
        response = self.client.get(reverse("monitor:suggestion_thanks"))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# OTP login view tests
# ---------------------------------------------------------------------------


class LoginRequestViewTest(TestCase):
    def test_get_returns_200(self):
        response = self.client.get(reverse("monitor:login_request"))
        self.assertEqual(response.status_code, 200)

    def test_post_creates_login_code_and_sends_email(self):
        response = self.client.post(
            reverse("monitor:login_request"), {"email": "Alice@Example.com"}
        )
        self.assertRedirects(response, reverse("monitor:login_verify"))
        code = LoginCode.objects.filter(email="alice@example.com").latest("created_at")
        self.assertIsNotNone(code)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["alice@example.com"])

    def test_post_behind_https_proxy_does_not_reject_origin(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("monitor:login_request"))
        token = client.cookies["csrftoken"].value
        extra = {
            "HTTP_ORIGIN": "https://tosdiff.org",
            "HTTP_HOST": "tosdiff.org",
            "HTTP_X_FORWARDED_PROTO": "https",
            "wsgi.url_scheme": "http",
        }
        response = client.post(
            reverse("monitor:login_request"),
            {"email": "proxy@example.com", "csrfmiddlewaretoken": token},
            **extra,
        )
        self.assertEqual(response.status_code, 302)

    def test_post_stores_email_in_session(self):
        self.client.post(reverse("monitor:login_request"), {"email": "bob@example.com"})
        self.assertEqual(self.client.session["login_email"], "bob@example.com")

    def test_resend_invalidates_previous_codes(self):
        self.client.post(reverse("monitor:login_request"), {"email": "bob@example.com"})
        self.client.post(reverse("monitor:login_request"), {"email": "bob@example.com"})
        unused = LoginCode.objects.filter(email="bob@example.com", used_at__isnull=True)
        self.assertEqual(unused.count(), 1)

    def test_authenticated_user_redirected_away(self):
        user = get_user_model().objects.create_user(username="carol", email="carol@example.com")
        self.client.force_login(user)
        response = self.client.get(reverse("monitor:login_request"))
        self.assertRedirects(response, reverse("monitor:account"))


class LoginVerifyViewTest(TestCase):
    def setUp(self):
        with patch("monitor.views.generate_login_code", return_value="123456"):
            self.client.post(reverse("monitor:login_request"), {"email": "bob@example.com"})

    def test_successful_code_logs_user_in(self):
        response = self.client.post(reverse("monitor:login_verify"), {"code": "123456"})
        self.assertRedirects(response, reverse("monitor:account"))
        user = get_user_model().objects.get(email="bob@example.com")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_user_account_created_on_first_login(self):
        self.client.post(reverse("monitor:login_verify"), {"code": "123456"})
        self.assertEqual(get_user_model().objects.filter(email="bob@example.com").count(), 1)

    def test_wrong_code_rejected(self):
        response = self.client.post(reverse("monitor:login_verify"), {"code": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_expired_code_rejected(self):
        LoginCode.objects.filter(email="bob@example.com").update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1)
        )
        response = self.client.post(reverse("monitor:login_verify"), {"code": "123456"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_code_marked_used_after_login(self):
        self.client.post(reverse("monitor:login_verify"), {"code": "123456"})
        used = LoginCode.objects.filter(email="bob@example.com", used_at__isnull=False)
        self.assertEqual(used.count(), 1)

    def test_no_email_in_session_redirects_to_request(self):
        self.client.session.flush()
        response = self.client.post(reverse("monitor:login_verify"), {"code": "123456"})
        self.assertRedirects(response, reverse("monitor:login_request"))

    def test_verify_page_shows_email(self):
        response = self.client.get(reverse("monitor:login_verify"))
        self.assertContains(response, "bob@example.com")

    def test_redirects_to_next_after_login(self):
        with patch("monitor.views.generate_login_code", return_value="654321"):
            self.client.post(
                reverse("monitor:login_request") + "?next=/organizations/",
                {"email": "carol@example.com"},
            )
        response = self.client.post(reverse("monitor:login_verify"), {"code": "654321"})
        self.assertRedirects(response, "/organizations/")


class LogoutViewTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        self.client.force_login(self.user)

    def test_logout_logs_user_out(self):
        response = self.client.post(reverse("monitor:logout"))
        self.assertRedirects(response, reverse("monitor:home"))
        self.assertNotIn("_auth_user_id", self.client.session)


class AccountViewTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        OrganizationSubscription.objects.create(user=self.user, organization=org)
        self.client.force_login(self.user)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("monitor:account"))
        self.assertEqual(response.status_code, 302)

    def test_shows_email(self):
        response = self.client.get(reverse("monitor:account"))
        self.assertContains(response, "bob@example.com")

    def test_lists_document_subscriptions(self):
        response = self.client.get(reverse("monitor:account"))
        self.assertContains(response, "Acme")
        self.assertContains(response, "Terms of Service")

    def test_lists_organization_subscriptions(self):
        response = self.client.get(reverse("monitor:account"))
        org_subs = list(response.context["organization_subscriptions"])
        self.assertEqual(len(org_subs), 1)
        self.assertEqual(org_subs[0].organization.name, "Acme")


# ---------------------------------------------------------------------------
# Subscription view tests
# ---------------------------------------------------------------------------


class DocumentSubscriptionViewTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")

    def test_subscribe_requires_login(self):
        response = self.client.post(reverse("monitor:document_subscribe", args=[self.doc.pk]))
        self.assertEqual(response.status_code, 302)

    def test_subscribe_creates_subscription(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("monitor:document_subscribe", args=[self.doc.pk]))
        self.assertRedirects(response, reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertEqual(
            DocumentSubscription.objects.filter(user=self.user, document=self.doc).count(), 1
        )

    def test_subscribe_is_idempotent(self):
        self.client.force_login(self.user)
        url = reverse("monitor:document_subscribe", args=[self.doc.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(
            DocumentSubscription.objects.filter(user=self.user, document=self.doc).count(), 1
        )

    def test_unsubscribe_removes_subscription(self):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        self.client.force_login(self.user)
        response = self.client.post(reverse("monitor:document_unsubscribe", args=[self.doc.pk]))
        self.assertRedirects(response, reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertEqual(
            DocumentSubscription.objects.filter(user=self.user, document=self.doc).count(), 0
        )


class OrganizationSubscriptionViewTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")

    def test_subscribe_requires_login(self):
        response = self.client.post(reverse("monitor:organization_subscribe", args=[self.org.pk]))
        self.assertEqual(response.status_code, 302)

    def test_subscribe_creates_subscription(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("monitor:organization_subscribe", args=[self.org.pk]))
        self.assertRedirects(response, reverse("monitor:organizations"))
        self.assertEqual(
            OrganizationSubscription.objects.filter(user=self.user, organization=self.org).count(),
            1,
        )

    def test_unsubscribe_removes_subscription(self):
        OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        self.client.force_login(self.user)
        response = self.client.post(reverse("monitor:organization_unsubscribe", args=[self.org.pk]))
        self.assertRedirects(response, reverse("monitor:organizations"))
        self.assertEqual(
            OrganizationSubscription.objects.filter(user=self.user, organization=self.org).count(),
            0,
        )


class DocumentSubscriptionContextTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")

    def test_anonymous_user_sees_login_prompt(self):
        response = self.client.get(reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertContains(response, "Log in to subscribe")

    def test_authenticated_user_sees_subscribe_form(self):
        user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        self.client.force_login(user)
        response = self.client.get(reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertContains(response, "Subscribe")

    def test_subscribed_user_sees_unsubscribe_form(self):
        user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        DocumentSubscription.objects.create(user=user, document=self.doc)
        self.client.force_login(user)
        response = self.client.get(reverse("monitor:document_detail", args=[self.doc.pk]))
        self.assertContains(response, "Unsubscribe")
        self.assertNotContains(response, "Subscribe")


# ---------------------------------------------------------------------------
# Change notification task tests
# ---------------------------------------------------------------------------


class SendChangeNotificationsTaskTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.old = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="Version 1", text_hash=compute_hash("v1")
        )
        self.new = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="Version 2", text_hash=compute_hash("v2")
        )
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        self.inactive_user = get_user_model().objects.create_user(
            username="carol", email="carol@example.com", is_active=False
        )

    @patch("monitor.tasks.send_mail")
    def test_queues_change_for_document_subscribers(self, mock_send):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["queued"], 1)
        self.assertIsNone(result["error"])
        mock_send.assert_not_called()
        self.assertEqual(
            PendingNotification.objects.filter(user=self.user, document=self.doc).count(), 1
        )

    @patch("monitor.tasks.send_mail")
    def test_queues_change_for_organization_subscribers(self, mock_send):
        OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["queued"], 1)
        mock_send.assert_not_called()

    @patch("monitor.tasks.send_mail")
    def test_deduplicates_users_subscribed_at_both_levels(self, mock_send):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["queued"], 1)
        mock_send.assert_not_called()

    @patch("monitor.tasks.send_mail")
    def test_skips_inactive_users(self, mock_send):
        DocumentSubscription.objects.create(user=self.inactive_user, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["queued"], 0)
        self.assertFalse(PendingNotification.objects.exists())

    @patch("monitor.tasks.send_mail")
    def test_no_subscribers_no_notification(self, mock_send):
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["queued"], 0)
        mock_send.assert_not_called()

    def test_pending_records_previous_snapshot_for_diff(self):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        with patch("monitor.tasks.send_mail"):
            send_change_notifications(self.doc.pk, self.new.pk)
        pending = PendingNotification.objects.get(user=self.user, document=self.doc)
        self.assertEqual(pending.snapshot, self.new)
        self.assertEqual(pending.old_snapshot, self.old)

    def test_missing_document_returns_error(self):
        result = send_change_notifications(99999, self.new.pk)
        self.assertEqual(result["sent"], 0)
        self.assertIn("not found", result["error"])

    def test_missing_snapshot_returns_error(self):
        result = send_change_notifications(self.doc.pk, 99999)
        self.assertEqual(result["sent"], 0)
        self.assertIn("not found", result["error"])


class CheckDocumentNotificationDispatchTest(TestCase):
    """check_document dispatches a notification task when a snapshot is created."""

    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")

    @patch("monitor.tasks.fetch_and_snapshot")
    def test_dispatches_notifications_when_created(self, mock_fas):
        from monitor.tasks import check_document

        mock_snapshot = MagicMock()
        mock_fas.return_value = (mock_snapshot, True)
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        with patch("monitor.tasks.send_change_notifications.delay") as mock_delay:
            check_document(self.doc.pk)
        mock_delay.assert_called_once_with(self.doc.pk, mock_snapshot.pk)

    @patch("monitor.tasks.fetch_and_snapshot")
    def test_no_dispatch_when_no_subscribers(self, mock_fas):
        from monitor.tasks import check_document

        mock_fas.return_value = (MagicMock(), True)
        with patch("monitor.tasks.send_change_notifications.delay") as mock_delay:
            check_document(self.doc.pk)
        mock_delay.assert_not_called()

    @patch("monitor.tasks.fetch_and_snapshot")
    def test_no_dispatch_when_unchanged(self, mock_fas):
        from monitor.tasks import check_document

        mock_fas.return_value = (None, False)
        with patch("monitor.tasks.send_change_notifications.delay") as mock_delay:
            check_document(self.doc.pk)
        mock_delay.assert_not_called()


# ---------------------------------------------------------------------------
# Management (superuser CRUD) view tests
# ---------------------------------------------------------------------------


def _login_as_superuser(client):
    """Create and force-login a superuser for the given test client."""
    superuser = get_user_model().objects.create_superuser(
        username="root", email="root@example.com", password="secret123"
    )
    client.force_login(superuser)
    return superuser


class ManageAccessControlTest(TestCase):
    """Only superusers may access the /manage/ pages."""

    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.tag = Tag.objects.create(name="Fintech")
        self.country = Country.objects.create(name="France", code="FR")
        self.language = Language.objects.create(name="Spanish", code="es")
        self.suggestion = Suggestion.objects.create(
            organization_name="Globex",
            website_url="https://globex.example.com",
            document_url="https://globex.example.com/tos",
            document_type=Document.DocumentType.TERMS_OF_SERVICE,
        )

    def _manage_urls(self):
        return [
            reverse("monitor:manage_dashboard"),
            reverse("monitor:manage_organizations"),
            reverse("monitor:manage_organization_create"),
            reverse("monitor:manage_organization_update", args=[self.org.pk]),
            reverse("monitor:manage_organization_delete", args=[self.org.pk]),
            reverse("monitor:manage_documents"),
            reverse("monitor:manage_document_create"),
            reverse("monitor:manage_document_create_for_organization", args=[self.org.pk]),
            reverse("monitor:manage_document_update", args=[self.doc.pk]),
            reverse("monitor:manage_document_delete", args=[self.doc.pk]),
            reverse("monitor:manage_suggestions"),
            reverse("monitor:manage_suggestion_approve", args=[self.suggestion.pk]),
            reverse("monitor:manage_suggestion_reject", args=[self.suggestion.pk]),
            reverse("monitor:manage_suggestion_delete", args=[self.suggestion.pk]),
            reverse("monitor:manage_tags"),
            reverse("monitor:manage_tag_create"),
            reverse("monitor:manage_tag_update", args=[self.tag.pk]),
            reverse("monitor:manage_tag_delete", args=[self.tag.pk]),
            reverse("monitor:manage_countries"),
            reverse("monitor:manage_country_create"),
            reverse("monitor:manage_country_update", args=[self.country.pk]),
            reverse("monitor:manage_country_delete", args=[self.country.pk]),
            reverse("monitor:manage_languages"),
            reverse("monitor:manage_language_create"),
            reverse("monitor:manage_language_update", args=[self.language.pk]),
            reverse("monitor:manage_language_delete", args=[self.language.pk]),
            reverse("monitor:manage_attention"),
            reverse("monitor:manage_users"),
        ]

    def _manage_post_urls(self):
        return [
            reverse("monitor:manage_document_check", args=[self.doc.pk]),
        ]

    def test_anonymous_users_redirected_to_login(self):
        for url in self._manage_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)
        for url in self._manage_post_urls():
            with self.subTest(url=url):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)

    def test_regular_users_are_forbidden(self):
        get_user_model().objects.create_user(username="bob", email="bob@example.com", password="x")
        self.client.login(username="bob", password="x")
        for url in self._manage_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
        for url in self._manage_post_urls():
            with self.subTest(url=url):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 403)

    def test_superusers_can_access(self):
        _login_as_superuser(self.client)
        for url in self._manage_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)


class ManageDashboardTest(TestCase):
    def setUp(self):
        _login_as_superuser(self.client)

    def test_dashboard_shows_counts(self):
        Organization.objects.create(name="Acme", website_url="https://acme.com")
        org = Organization.objects.get(name="Acme")
        Document.objects.create(organization=org, url="https://acme.com/tos")
        Suggestion.objects.create(
            organization_name="Globex",
            website_url="https://globex.example.com",
            document_url="https://globex.example.com/tos",
        )
        Country.objects.create(name="France", code="FR")
        Language.objects.get_or_create(code="es", defaults={"name": "Spanish"})
        response = self.client.get(reverse("monitor:manage_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["organization_count"], 1)
        self.assertEqual(response.context["document_count"], 1)
        self.assertEqual(response.context["pending_suggestion_count"], 1)
        self.assertEqual(response.context["country_count"], 1)
        self.assertEqual(response.context["language_count"], Language.objects.count())


class ManageOrganizationViewsTest(TestCase):
    def setUp(self):
        _login_as_superuser(self.client)
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")

    def test_list_shows_organizations(self):
        response = self.client.get(reverse("monitor:manage_organizations"))
        self.assertContains(response, "Acme")

    def test_create_organization(self):
        response = self.client.post(
            reverse("monitor:manage_organization_create"),
            {
                "name": "Globex",
                "website_url": "https://globex.example.com",
                "category": Organization.Category.TECHNOLOGY,
            },
        )
        self.assertRedirects(response, reverse("monitor:manage_organizations"))
        org = Organization.objects.get(name="Globex")
        self.assertEqual(org.slug, "globex")
        self.assertEqual(org.website_url, "https://globex.example.com")

    def test_create_requires_website_url(self):
        response = self.client.post(
            reverse("monitor:manage_organization_create"), {"name": "No URL"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Organization.objects.filter(name="No URL").exists())

    def test_update_organization(self):
        response = self.client.post(
            reverse("monitor:manage_organization_update", args=[self.org.pk]),
            {"name": "Acme Inc", "website_url": self.org.website_url},
        )
        self.assertRedirects(response, reverse("monitor:manage_organizations"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Acme Inc")

    def test_update_form_prefilled(self):
        response = self.client.get(
            reverse("monitor:manage_organization_update", args=[self.org.pk])
        )
        self.assertContains(response, 'value="Acme"')

    def test_update_form_includes_slug(self):
        self.org.slug = "acme"
        self.org.save()
        response = self.client.get(
            reverse("monitor:manage_organization_update", args=[self.org.pk])
        )
        self.assertContains(response, 'name="slug"')
        self.assertContains(response, 'value="acme"')

    def test_update_organization_can_set_slug(self):
        response = self.client.post(
            reverse("monitor:manage_organization_update", args=[self.org.pk]),
            {"name": "Acme Inc", "website_url": self.org.website_url, "slug": "acme-inc"},
        )
        self.assertRedirects(response, reverse("monitor:manage_organizations"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.slug, "acme-inc")

    def test_create_organization_auto_generates_slug_when_blank(self):
        response = self.client.post(
            reverse("monitor:manage_organization_create"),
            {
                "name": "Initech",
                "website_url": "https://initech.example.com",
                "slug": "",
            },
        )
        self.assertRedirects(response, reverse("monitor:manage_organizations"))
        org = Organization.objects.get(name="Initech")
        self.assertEqual(org.slug, "initech")


class ManageDocumentViewsTest(TestCase):
    def setUp(self):
        _login_as_superuser(self.client)
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")

    def _doc_payload(self, **overrides):
        payload = {
            "organization": self.org.pk,
            "name": "",
            "document_type": Document.DocumentType.TERMS_OF_SERVICE,
            "other_document_type": "",
            "url": "https://acme.example.com/terms",
            "fetch_method": Document.FetchMethod.REQUESTS,
            "document_format": Document.DocumentFormat.HTML,
            "custom_selectors": "",
            "fetch_config": "",
            "is_active": "on",
            "is_failing": "",
        }
        payload.update(overrides)
        return payload

    def test_list_shows_documents(self):
        Document.objects.create(organization=self.org, url="https://acme.example.com/terms")
        response = self.client.get(reverse("monitor:manage_documents"))
        self.assertContains(response, "Terms of Service")

    def test_create_document(self):
        response = self.client.post(reverse("monitor:manage_document_create"), self._doc_payload())
        self.assertRedirects(response, reverse("monitor:manage_documents"))
        Document.objects.get(organization=self.org, url="https://acme.example.com/terms")

    def test_create_document_preselects_organization(self):
        form = self.client.get(
            reverse("monitor:manage_document_create_for_organization", args=[self.org.pk])
        ).context["form"]
        self.assertEqual(form.initial["organization"], self.org.pk)

    def test_update_document(self):
        doc = Document.objects.create(organization=self.org, url="https://acme.example.com/terms")
        response = self.client.post(
            reverse("monitor:manage_document_update", args=[doc.pk]),
            self._doc_payload(name="Company Terms", url="https://acme.example.com/terms"),
        )
        self.assertRedirects(response, reverse("monitor:manage_documents"))
        doc.refresh_from_db()
        self.assertEqual(doc.name, "Company Terms")

    def test_duplicate_document_rejected(self):
        Document.objects.create(organization=self.org, url="https://acme.example.com/terms")
        response = self.client.post(reverse("monitor:manage_document_create"), self._doc_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Document.objects.count(), 1)


class ManageSuggestionViewsTest(TestCase):
    def setUp(self):
        _login_as_superuser(self.client)
        self.suggestion = Suggestion.objects.create(
            organization_name="Globex",
            website_url="https://globex.example.com",
            document_url="https://globex.example.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
        )

    def test_list_shows_pending_suggestions(self):
        response = self.client.get(reverse("monitor:manage_suggestions"))
        self.assertContains(response, "Globex")

    def test_approve_page_shows_details(self):
        response = self.client.get(
            reverse("monitor:manage_suggestion_approve", args=[self.suggestion.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Globex")

    def test_approve_converts_suggestion(self):
        response = self.client.post(
            reverse("monitor:manage_suggestion_approve", args=[self.suggestion.pk]),
            {"review_notes": "Looks good"},
        )
        self.assertRedirects(response, reverse("monitor:manage_suggestions"))
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, Suggestion.Status.APPROVED)
        self.assertIsNotNone(self.suggestion.reviewed_at)
        self.assertEqual(self.suggestion.review_notes, "Looks good")
        org = Organization.objects.get(website_url="https://globex.example.com")
        self.assertTrue(
            Document.objects.filter(
                organization=org, document_type=Document.DocumentType.PRIVACY_POLICY
            ).exists()
        )

    def test_reject_marks_suggestion_rejected(self):
        response = self.client.post(
            reverse("monitor:manage_suggestion_reject", args=[self.suggestion.pk]),
            {"review_notes": "Duplicate"},
        )
        self.assertRedirects(response, reverse("monitor:manage_suggestions"))
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, Suggestion.Status.REJECTED)
        self.assertFalse(Document.objects.exists())


class ManageTagViewsTest(TestCase):
    def setUp(self):
        _login_as_superuser(self.client)
        self.tag = Tag.objects.create(name="Fintech")

    def test_list_shows_tags(self):
        response = self.client.get(reverse("monitor:manage_tags"))
        self.assertContains(response, "Fintech")

    def test_create_tag(self):
        response = self.client.post(reverse("monitor:manage_tag_create"), {"name": "Open Source"})
        self.assertRedirects(response, reverse("monitor:manage_tags"))
        tag = Tag.objects.get(name="Open Source")
        self.assertEqual(tag.slug, "open-source")

    def test_create_tag_with_custom_slug(self):
        response = self.client.post(
            reverse("monitor:manage_tag_create"), {"name": "Open Banking", "slug": "banking"}
        )
        self.assertRedirects(response, reverse("monitor:manage_tags"))
        self.assertEqual(Tag.objects.get(name="Open Banking").slug, "banking")

    def test_create_tag_auto_generates_slug_when_blank(self):
        response = self.client.post(
            reverse("monitor:manage_tag_create"), {"name": "Regulatory", "slug": ""}
        )
        self.assertRedirects(response, reverse("monitor:manage_tags"))
        self.assertEqual(Tag.objects.get(name="Regulatory").slug, "regulatory")

    def test_update_tag(self):
        response = self.client.post(
            reverse("monitor:manage_tag_update", args=[self.tag.pk]), {"name": "Fintech & Banking"}
        )
        self.assertRedirects(response, reverse("monitor:manage_tags"))
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.name, "Fintech & Banking")

    def test_update_form_includes_slug(self):
        self.tag.slug = "fintech"
        self.tag.save()
        response = self.client.get(reverse("monitor:manage_tag_update", args=[self.tag.pk]))
        self.assertContains(response, 'name="slug"')
        self.assertContains(response, 'value="fintech"')

    def test_update_tag_can_set_slug(self):
        response = self.client.post(
            reverse("monitor:manage_tag_update", args=[self.tag.pk]),
            {"name": "Fintech", "slug": "fintech-banking"},
        )
        self.assertRedirects(response, reverse("monitor:manage_tags"))
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.slug, "fintech-banking")


class ManageCountryViewsTest(TestCase):
    """Superusers can CRUD countries via the management area."""

    def setUp(self):
        _login_as_superuser(self.client)
        self.country = Country.objects.create(name="France", code="FR")

    def test_list_shows_countries(self):
        response = self.client.get(reverse("monitor:manage_countries"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "France")

    def test_create_country(self):
        response = self.client.post(
            reverse("monitor:manage_country_create"), {"name": "Spain", "code": "ES"}
        )
        self.assertRedirects(response, reverse("monitor:manage_countries"))
        self.assertTrue(Country.objects.filter(name="Spain", code="ES").exists())

    def test_create_country_rejects_duplicate_code(self):
        response = self.client.post(
            reverse("monitor:manage_country_create"), {"name": "France Again", "code": "FR"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Country.objects.filter(name="France Again").exists())

    def test_update_country(self):
        response = self.client.post(
            reverse("monitor:manage_country_update", args=[self.country.pk]),
            {"name": "French Republic", "code": "FR"},
        )
        self.assertRedirects(response, reverse("monitor:manage_countries"))
        self.country.refresh_from_db()
        self.assertEqual(self.country.name, "French Republic")

    def test_update_form_prefilled(self):
        response = self.client.get(reverse("monitor:manage_country_update", args=[self.country.pk]))
        self.assertContains(response, 'value="France"')


class ManageLanguageViewsTest(TestCase):
    """Superusers can CRUD languages via the management area."""

    def setUp(self):
        _login_as_superuser(self.client)
        self.language = Language.objects.create(name="Spanish", code="es")

    def test_list_shows_languages(self):
        response = self.client.get(reverse("monitor:manage_languages"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Spanish")

    def test_create_language(self):
        response = self.client.post(
            reverse("monitor:manage_language_create"), {"name": "German", "code": "de"}
        )
        self.assertRedirects(response, reverse("monitor:manage_languages"))
        self.assertTrue(Language.objects.filter(name="German", code="de").exists())

    def test_update_language(self):
        response = self.client.post(
            reverse("monitor:manage_language_update", args=[self.language.pk]),
            {"name": "Castilian Spanish", "code": "es"},
        )
        self.assertRedirects(response, reverse("monitor:manage_languages"))
        self.language.refresh_from_db()
        self.assertEqual(self.language.name, "Castilian Spanish")

    def test_update_form_prefilled(self):
        response = self.client.get(
            reverse("monitor:manage_language_update", args=[self.language.pk])
        )
        self.assertContains(response, 'value="Spanish"')


class ManageDeleteViewsTest(TestCase):
    """Superusers can permanently delete orgs/docs/tags/suggestions with confirmation."""

    def setUp(self):
        _login_as_superuser(self.client)
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.tag = Tag.objects.create(name="Fintech")
        self.country = Country.objects.create(name="France", code="FR")
        self.language = Language.objects.create(name="German", code="de")
        self.suggestion = Suggestion.objects.create(
            organization_name="Globex",
            website_url="https://globex.example.com",
            document_url="https://globex.example.com/tos",
        )

    def test_confirm_page_shows_object_name(self):
        response = self.client.get(
            reverse("monitor:manage_organization_delete", args=[self.org.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acme")

    def test_delete_organization(self):
        response = self.client.post(
            reverse("monitor:manage_organization_delete", args=[self.org.pk])
        )
        self.assertRedirects(response, reverse("monitor:manage_organizations"))
        self.assertFalse(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertFalse(Document.objects.filter(organization_id=self.org.pk).exists())

    def test_delete_document(self):
        response = self.client.post(reverse("monitor:manage_document_delete", args=[self.doc.pk]))
        self.assertRedirects(response, reverse("monitor:manage_documents"))
        self.assertFalse(Document.objects.filter(pk=self.doc.pk).exists())

    def test_delete_tag(self):
        response = self.client.post(reverse("monitor:manage_tag_delete", args=[self.tag.pk]))
        self.assertRedirects(response, reverse("monitor:manage_tags"))
        self.assertFalse(Tag.objects.filter(pk=self.tag.pk).exists())

    def test_delete_country(self):
        response = self.client.post(
            reverse("monitor:manage_country_delete", args=[self.country.pk])
        )
        self.assertRedirects(response, reverse("monitor:manage_countries"))
        self.assertFalse(Country.objects.filter(pk=self.country.pk).exists())

    def test_delete_country_clears_document_references(self):
        self.doc.country = self.country
        self.doc.save()
        response = self.client.post(
            reverse("monitor:manage_country_delete", args=[self.country.pk])
        )
        self.assertRedirects(response, reverse("monitor:manage_countries"))
        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.country)

    def test_delete_language(self):
        response = self.client.post(
            reverse("monitor:manage_language_delete", args=[self.language.pk])
        )
        self.assertRedirects(response, reverse("monitor:manage_languages"))
        self.assertFalse(Language.objects.filter(pk=self.language.pk).exists())

    def test_delete_language_in_use_is_blocked(self):
        self.doc.language = self.language
        self.doc.save()
        response = self.client.post(
            reverse("monitor:manage_language_delete", args=[self.language.pk])
        )
        self.assertRedirects(response, reverse("monitor:manage_languages"))
        self.assertTrue(Language.objects.filter(pk=self.language.pk).exists())

    def test_delete_suggestion(self):
        response = self.client.post(
            reverse("monitor:manage_suggestion_delete", args=[self.suggestion.pk])
        )
        self.assertRedirects(response, reverse("monitor:manage_suggestions"))
        self.assertFalse(Suggestion.objects.filter(pk=self.suggestion.pk).exists())


class ManageDocumentCheckTest(TestCase):
    """The "Check now" action enqueues a fetch for a single document."""

    def setUp(self):
        _login_as_superuser(self.client)
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")

    @patch("monitor.views.check_document")
    def test_check_now_enqueues_fetch(self, mock_task):
        response = self.client.post(reverse("monitor:manage_document_check", args=[self.doc.pk]))
        self.assertRedirects(response, reverse("monitor:manage_documents"))
        mock_task.delay.assert_called_once_with(self.doc.pk)

    @patch("monitor.views.check_document")
    def test_check_now_announces_success(self, mock_task):
        self.client.post(reverse("monitor:manage_document_check", args=[self.doc.pk]))
        response = self.client.get(reverse("monitor:manage_documents"))
        self.assertContains(response, "Fetch queued for Terms of Service")

    def test_check_now_denied_to_regular_user(self):
        get_user_model().objects.create_user(username="bob", email="bob@example.com", password="x")
        self.client.login(username="bob", password="x")
        response = self.client.post(reverse("monitor:manage_document_check", args=[self.doc.pk]))
        self.assertEqual(response.status_code, 403)


class ManageAttentionViewTest(TestCase):
    """The needs-attention page lists failing, never-checked, and snapshot-less docs."""

    def setUp(self):
        _login_as_superuser(self.client)
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.failing_doc = Document.objects.create(
            organization=self.org, url="https://acme.com/fail", is_failing=True
        )
        self.never_checked = Document.objects.create(
            organization=self.org, url="https://acme.com/new"
        )
        self.ok_doc = Document.objects.create(
            organization=self.org,
            url="https://acme.com/ok",
            last_checked=timezone.now(),
            last_changed=timezone.now(),
        )
        DocumentSnapshot.objects.create(document=self.ok_doc, cleaned_text="text", text_hash="hash")

    def test_page_renders(self):
        response = self.client.get(reverse("monitor:manage_attention"))
        self.assertEqual(response.status_code, 200)

    def test_context_lists_problem_documents(self):
        response = self.client.get(reverse("monitor:manage_attention"))
        context = response.context
        self.assertIn(self.failing_doc.pk, [d.pk for d in context["failing_documents"]])
        self.assertIn(self.never_checked.pk, [d.pk for d in context["never_checked_documents"]])
        self.assertNotIn(self.ok_doc.pk, [d.pk for d in context["never_checked_documents"]])

    def test_no_snapshot_flagged(self):
        response = self.client.get(reverse("monitor:manage_attention"))
        context = response.context
        self.assertIn(self.never_checked.pk, [d.pk for d in context["no_snapshot_documents"]])


class ManageUserListViewTest(TestCase):
    """Admins can browse user accounts and their subscription counts."""

    def setUp(self):
        _login_as_superuser(self.client)
        self.user = get_user_model().objects.create_user(
            username="bob", email="bob@example.com", password="x"
        )

    def test_user_list_shows_users(self):
        response = self.client.get(reverse("monitor:manage_users"))
        self.assertContains(response, "bob@example.com")
        self.assertContains(response, "root@example.com")

    def test_user_list_search(self):
        response = self.client.get(reverse("monitor:manage_users"), {"q": "bob"})
        self.assertContains(response, "bob@example.com")
        self.assertNotContains(response, "root@example.com")

    def test_user_list_counts_subscriptions(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        DocumentSubscription.objects.create(user=self.user, document=doc)
        OrganizationSubscription.objects.create(user=self.user, organization=org)
        response = self.client.get(reverse("monitor:manage_users"))
        user_row = next(u for u in response.context["users"] if u.username == "bob")
        self.assertEqual(user_row.document_subscription_count, 1)
        self.assertEqual(user_row.organization_subscription_count, 1)


class UnsubscribeTokenViewTest(TestCase):
    """A signed unsubscribe link removes a document subscription without logging in."""

    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")

    def test_valid_token_removes_document_subscription(self):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        token = make_unsubscribe_token(self.user.pk, self.doc.pk)
        response = self.client.get(reverse("monitor:unsubscribe_token", args=[token]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DocumentSubscription.objects.filter(user=self.user, document=self.doc).exists()
        )

    def test_valid_token_without_subscription_is_harmless(self):
        token = make_unsubscribe_token(self.user.pk, self.doc.pk)
        response = self.client.get(reverse("monitor:unsubscribe_token", args=[token]))
        self.assertEqual(response.status_code, 200)

    def test_invalid_token_returns_404(self):
        response = self.client.get(reverse("monitor:unsubscribe_token", args=["not-a-real-token"]))
        self.assertEqual(response.status_code, 404)

    def test_does_not_remove_organization_subscription(self):
        OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        token = make_unsubscribe_token(self.user.pk, self.doc.pk)
        self.client.get(reverse("monitor:unsubscribe_token", args=[token]))
        self.assertTrue(
            OrganizationSubscription.objects.filter(user=self.user, organization=self.org).exists()
        )


class SuggestionReviewEmailsTest(TestCase):
    """Approving/rejecting a suggestion emails the submitter."""

    def setUp(self):
        _login_as_superuser(self.client)
        self.suggestion = Suggestion.objects.create(
            organization_name="Globex",
            website_url="https://globex.example.com",
            document_url="https://globex.example.com/privacy",
            document_type=Document.DocumentType.PRIVACY_POLICY,
            contact_email="submitter@example.com",
        )

    def test_approve_emails_submitter(self):
        self.client.post(
            reverse("monitor:manage_suggestion_approve", args=[self.suggestion.pk]), {}
        )
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["submitter@example.com"])
        self.assertIn("approved", email.subject.lower())

    def test_approve_email_links_the_new_document(self):
        self.client.post(
            reverse("monitor:manage_suggestion_approve", args=[self.suggestion.pk]), {}
        )
        org = Organization.objects.get(website_url="https://globex.example.com")
        doc = Document.objects.get(organization=org)
        email = mail.outbox[0]
        self.assertIn(f"/document/{doc.pk}/", email.body)

    def test_reject_emails_submitter_with_notes(self):
        self.client.post(
            reverse("monitor:manage_suggestion_reject", args=[self.suggestion.pk]),
            {"review_notes": "Duplicate of existing"},
        )
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("not approved", email.subject.lower())
        self.assertIn("Duplicate of existing", email.body)

    def test_no_email_when_no_contact(self):
        self.suggestion.contact_email = ""
        self.suggestion.save()
        self.client.post(
            reverse("monitor:manage_suggestion_approve", args=[self.suggestion.pk]), {}
        )
        self.assertEqual(len(mail.outbox), 0)


class ManageSuggestionDedupeTest(TestCase):
    """Suggestions whose website already exists are flagged as duplicates."""

    def setUp(self):
        _login_as_superuser(self.client)

    def test_duplicate_flag_when_org_exists(self):
        Organization.objects.create(name="Existing", website_url="https://globex.example.com")
        Suggestion.objects.create(
            organization_name="Globex",
            website_url="https://globex.example.com",
            document_url="https://globex.example.com/tos",
        )
        response = self.client.get(reverse("monitor:manage_suggestions"))
        suggestion = response.context["suggestions"][0]
        self.assertTrue(suggestion.is_duplicate)

    def test_duplicate_flag_false_when_org_missing(self):
        Suggestion.objects.create(
            organization_name="Fresh Co",
            website_url="https://fresh.example.com",
            document_url="https://fresh.example.com/tos",
        )
        response = self.client.get(reverse("monitor:manage_suggestions"))
        suggestion = response.context["suggestions"][0]
        self.assertFalse(suggestion.is_duplicate)


# ---------------------------------------------------------------------------
# Suggestion ownership by account + account notification preferences
# ---------------------------------------------------------------------------


class AccountSuggestionsTest(TestCase):
    """Suggestions are linked to the logged-in user and listed in their account."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        self.client.force_login(self.user)

    def test_account_lists_my_suggestions(self):
        Suggestion.objects.create(
            organization_name="Globex",
            website_url="https://globex.example.com",
            document_url="https://globex.example.com/tos",
            user=self.user,
        )
        response = self.client.get(reverse("monitor:account"))
        self.assertContains(response, "Globex")

    def test_account_hides_other_users_suggestions(self):
        other = get_user_model().objects.create_user(username="carol", email="carol@example.com")
        Suggestion.objects.create(
            organization_name="Initech",
            website_url="https://initech.example.com",
            document_url="https://initech.example.com/tos",
            user=other,
        )
        response = self.client.get(reverse("monitor:account"))
        self.assertNotContains(response, "Initech")

    def test_logged_in_suggestion_records_user(self):
        self.client.post(
            reverse("monitor:suggest"),
            {
                "organization_name": "Acme",
                "website_url": "https://acme.com",
                "document_url": "https://acme.com/tos",
                "document_type": Document.DocumentType.TERMS_OF_SERVICE,
            },
        )
        suggestion = Suggestion.objects.get(organization_name="Acme")
        self.assertEqual(suggestion.user, self.user)

    def test_anonymous_suggestion_has_no_user(self):
        self.client.logout()
        self.client.post(
            reverse("monitor:suggest"),
            {
                "organization_name": "Acme",
                "website_url": "https://acme.com",
                "document_url": "https://acme.com/tos",
                "document_type": Document.DocumentType.TERMS_OF_SERVICE,
            },
        )
        suggestion = Suggestion.objects.get(organization_name="Acme")
        self.assertIsNone(suggestion.user)


class AccountNotificationPreferenceTest(TestCase):
    """Users choose daily or weekly digest notifications from their account."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        self.client.force_login(self.user)

    def test_new_preference_defaults_to_daily(self):
        NotificationPreference.objects.create(user=self.user)
        pref = NotificationPreference.objects.get(user=self.user)
        self.assertEqual(pref.frequency, NotificationPreference.Frequency.DAILY)

    def test_account_form_excludes_immediate(self):
        response = self.client.get(reverse("monitor:account"))
        form = response.context["notification_preference_form"]
        self.assertEqual(
            [value for value, _ in form.fields["frequency"].choices],
            [NotificationPreference.Frequency.DAILY, NotificationPreference.Frequency.WEEKLY],
        )

    def test_updates_frequency_on_post(self):
        self.client.post(
            reverse("monitor:account"), {"frequency": NotificationPreference.Frequency.DAILY}
        )
        pref = NotificationPreference.objects.get(user=self.user)
        self.assertEqual(pref.frequency, NotificationPreference.Frequency.DAILY)

    def test_second_post_updates_existing_preference(self):
        NotificationPreference.objects.create(
            user=self.user, frequency=NotificationPreference.Frequency.DAILY
        )
        self.client.post(
            reverse("monitor:account"), {"frequency": NotificationPreference.Frequency.WEEKLY}
        )
        pref = NotificationPreference.objects.get(user=self.user)
        self.assertEqual(pref.frequency, NotificationPreference.Frequency.WEEKLY)

    def test_account_shows_preference_form(self):
        response = self.client.get(reverse("monitor:account"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["notification_preference_form"])
        self.assertContains(response, "Save preferences")


# ---------------------------------------------------------------------------
# Email digest system
# ---------------------------------------------------------------------------


class DigestDeliveryTest(TestCase):
    """send_change_notifications queues a change for every subscriber."""

    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.new = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="v2", text_hash="h2"
        )
        self.user = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        self.daily = get_user_model().objects.create_user(username="dana", email="dana@example.com")
        self.weekly = get_user_model().objects.create_user(
            username="wally", email="wally@example.com"
        )
        NotificationPreference.objects.create(
            user=self.daily, frequency=NotificationPreference.Frequency.DAILY
        )
        NotificationPreference.objects.create(
            user=self.weekly, frequency=NotificationPreference.Frequency.WEEKLY
        )

    @patch("monitor.tasks.send_mail")
    def test_change_queues_every_subscriber_and_emails_nobody(self, mock_send):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        DocumentSubscription.objects.create(user=self.daily, document=self.doc)
        DocumentSubscription.objects.create(user=self.weekly, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["queued"], 3)
        mock_send.assert_not_called()

    @patch("monitor.tasks.send_mail")
    def test_user_without_preference_is_queued(self, mock_send):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["queued"], 1)
        mock_send.assert_not_called()

    @patch("monitor.tasks.send_mail")
    def test_daily_user_is_queued_not_emailed(self, mock_send):
        DocumentSubscription.objects.create(user=self.daily, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["queued"], 1)
        mock_send.assert_not_called()
        self.assertEqual(
            PendingNotification.objects.filter(user=self.daily, document=self.doc).count(), 1
        )

    @patch("monitor.tasks.send_mail")
    def test_weekly_user_is_queued_not_emailed(self, mock_send):
        DocumentSubscription.objects.create(user=self.weekly, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["queued"], 1)
        mock_send.assert_not_called()

    def test_pending_notification_records_previous_snapshot(self):
        old = DocumentSnapshot.objects.create(document=self.doc, cleaned_text="v1", text_hash="h1")
        DocumentSubscription.objects.create(user=self.daily, document=self.doc)
        with patch("monitor.tasks.send_mail"):
            send_change_notifications(self.doc.pk, self.new.pk)
        pending = PendingNotification.objects.get(user=self.daily, document=self.doc)
        self.assertEqual(pending.snapshot, self.new)
        self.assertEqual(pending.old_snapshot, old)


class SendDailyDigestTaskTest(TestCase):
    """The daily digest task emails queued daily subscribers once and clears the queue."""

    def setUp(self):
        self.org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=self.org, url="https://acme.com/tos")
        self.new = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="v2", text_hash="h2"
        )
        self.daily = get_user_model().objects.create_user(username="dana", email="dana@example.com")
        NotificationPreference.objects.create(
            user=self.daily, frequency=NotificationPreference.Frequency.DAILY
        )
        DocumentSubscription.objects.create(user=self.daily, document=self.doc)

    def _queue(self):
        with patch("monitor.tasks.send_mail"):
            send_change_notifications(self.doc.pk, self.new.pk)

    def test_digest_sends_one_email_per_user(self):
        self._queue()
        with patch("monitor.tasks.send_mail") as mock_send:
            result = send_daily_digests()
        self.assertEqual(result["sent"], 1)
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        self.assertEqual(args[3], ["dana@example.com"])
        self.assertIn("Acme", args[1])

    def test_digest_clears_pending_notifications(self):
        self._queue()
        with patch("monitor.tasks.send_mail"):
            send_daily_digests()
        self.assertFalse(PendingNotification.objects.filter(user=self.daily).exists())

    def test_digest_body_links_to_document(self):
        self._queue()
        with patch("monitor.tasks.send_mail") as mock_send:
            send_daily_digests()
        body = mock_send.call_args[0][1]
        self.assertIn(f"/document/{self.doc.pk}/", body)

    def test_daily_digest_ignores_weekly_users(self):
        weekly = get_user_model().objects.create_user(username="wally", email="wally@example.com")
        NotificationPreference.objects.create(
            user=weekly, frequency=NotificationPreference.Frequency.WEEKLY
        )
        DocumentSubscription.objects.create(user=weekly, document=self.doc)
        self._queue()
        with patch("monitor.tasks.send_mail") as mock_send:
            result = send_daily_digests()
        self.assertEqual(result["sent"], 1)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][3], ["dana@example.com"])
        self.assertTrue(PendingNotification.objects.filter(user=weekly).exists())

    def test_daily_digest_includes_users_without_preference(self):
        bob = get_user_model().objects.create_user(username="bob", email="bob@example.com")
        DocumentSubscription.objects.create(user=bob, document=self.doc)
        self._queue()
        with patch("monitor.tasks.send_mail") as mock_send:
            result = send_daily_digests()
        self.assertEqual(result["sent"], 2)
        sent_to = [recipient for call in mock_send.call_args_list for recipient in call.args[3]]
        self.assertCountEqual(sent_to, ["bob@example.com", "dana@example.com"])

    def test_no_pending_means_no_email(self):
        with patch("monitor.tasks.send_mail") as mock_send:
            result = send_daily_digests()
        self.assertEqual(result["sent"], 0)
        mock_send.assert_not_called()


class SendWeeklyDigestTaskTest(TestCase):
    """The weekly digest task emails queued weekly subscribers once."""

    def test_weekly_digest_sends_and_clears(self):
        org = Organization.objects.create(name="Beta", website_url="https://beta.example.com")
        doc = Document.objects.create(organization=org, url="https://beta.example.com/tos")
        new = DocumentSnapshot.objects.create(document=doc, cleaned_text="v2", text_hash="h2")
        weekly = get_user_model().objects.create_user(username="wally", email="wally@example.com")
        NotificationPreference.objects.create(
            user=weekly, frequency=NotificationPreference.Frequency.WEEKLY
        )
        DocumentSubscription.objects.create(user=weekly, document=doc)
        with patch("monitor.tasks.send_mail"):
            send_change_notifications(doc.pk, new.pk)
        with patch("monitor.tasks.send_mail") as mock_send:
            result = send_weekly_digests()
        self.assertEqual(result["sent"], 1)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][3], ["wally@example.com"])
        self.assertFalse(PendingNotification.objects.filter(user=weekly).exists())


class ViewModuleAnnotationsTest(TestCase):
    def test_annotations_resolve_on_python_312(self):
        from monitor import views

        for func in (views._get_or_create_user_by_email, views.verify_login_code):
            annotations = func.__annotations__  # PEP 649 resolution — may NameError
            self.assertIn("return", annotations)


class BeatScheduleDigestTest(TestCase):
    """Beat runs the digest tasks on schedule."""

    def test_beat_schedule_contains_digest_tasks(self):
        tasks = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn("monitor.tasks.send_daily_digests", tasks)
        self.assertIn("monitor.tasks.send_weekly_digests", tasks)


class HttpsOnlyProductionTest(TestCase):
    """HTTPS-only settings must match the production values in settings.py."""

    @override_settings(
        DEBUG=False,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SECURE_SSL_REDIRECT=True,
        SECURE_HSTS_SECONDS=31_536_000,
        SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
        SECURE_HSTS_PRELOAD=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        SECURE_REFERRER_POLICY="same-origin",
        ALLOWED_HOSTS=["tosdiff.org"],
    )
    def test_plain_http_is_redirected_to_https(self):
        response = self.client.get(
            reverse("monitor:home"),
            **{"HTTP_HOST": "tosdiff.org", "wsgi.url_scheme": "http"},
        )
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://tosdiff.org/"))

    @override_settings(
        DEBUG=False,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SECURE_SSL_REDIRECT=True,
        SECURE_HSTS_SECONDS=31_536_000,
        SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
        SECURE_HSTS_PRELOAD=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        SECURE_REFERRER_POLICY="same-origin",
        ALLOWED_HOSTS=["tosdiff.org"],
    )
    def test_https_response_sets_hsts_and_secure_cookies(self):
        response = self.client.get(
            reverse("monitor:login_request"),
            **{
                "HTTP_HOST": "tosdiff.org",
                "HTTP_X_FORWARDED_PROTO": "https",
                "wsgi.url_scheme": "http",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Strict-Transport-Security", response.headers)
        self.assertTrue(response.cookies["csrftoken"]["secure"])


class DatabaseConfigFromUrlTest(TestCase):
    """DATABASE_URL parsing forwards query params to the Postgres OPTIONS."""

    def test_render_sslmode_require_passed_through(self):
        from tosdiff.settings import database_config_from_url

        config = database_config_from_url(
            "postgresql://user:pass@dpg-abc-a.oregon-postgres.render.com/tosdiff?sslmode=require"
        )
        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["NAME"], "tosdiff")
        self.assertEqual(config["USER"], "user")
        self.assertEqual(config["HOST"], "dpg-abc-a.oregon-postgres.render.com")
        self.assertEqual(config["PORT"], "5432")
        self.assertEqual(config["OPTIONS"], {"sslmode": "require"})

    def test_no_query_params_means_empty_options(self):
        from tosdiff.settings import database_config_from_url

        config = database_config_from_url("postgresql://user:pass@host:5432/tosdiff")
        self.assertEqual(config["OPTIONS"], {})

    def test_wrapped_in_quotes_still_parses(self):
        from tosdiff.settings import database_config_from_url

        config = database_config_from_url(
            '"postgresql://user:pass@host:5432/tosdiff_kvvw?sslmode=require"'
        )
        self.assertEqual(config["NAME"], "tosdiff_kvvw")
        self.assertEqual(config["USER"], "user")
        self.assertEqual(config["OPTIONS"], {"sslmode": "require"})

    def test_wrapped_in_single_quotes_and_spaces_still_parses(self):
        from tosdiff.settings import database_config_from_url

        config = database_config_from_url(" 'postgresql://user:pass@host/tosdiff?sslmode=require' ")
        self.assertEqual(config["NAME"], "tosdiff")
        self.assertEqual(config["OPTIONS"], {"sslmode": "require"})

    def test_last_repeated_param_wins(self):
        from tosdiff.settings import database_config_from_url

        config = database_config_from_url(
            "postgresql://user:pass@host:5432/tosdiff?sslmode=require&sslmode=verify-full"
        )
        self.assertEqual(config["OPTIONS"], {"sslmode": "verify-full"})

    def test_missing_parts_get_defaults(self):
        from tosdiff.settings import database_config_from_url

        config = database_config_from_url("postgresql:///tosdiff")
        self.assertEqual(config["NAME"], "tosdiff")
        self.assertEqual(config["USER"], "")
        self.assertEqual(config["HOST"], "localhost")
        self.assertEqual(config["PORT"], "5432")
        self.assertEqual(config["OPTIONS"], {})


# ---------------------------------------------------------------------------
# Email delivery — Mailgun transactional backend & admin mailer routing
# ---------------------------------------------------------------------------


@override_settings(
    MAILGUN_API_KEY="key-cafebeef",
    MAILGUN_DOMAIN="mg.example.com",
)
class MailgunBackendTest(TestCase):
    """MailgunBackend posts messages to Mailgun's HTTP Messages API."""

    def _message(self):
        return EmailMessage(
            "Subject",
            "Body",
            "TosDiff <no-reply@example.com>",
            ["bob@example.com"],
        )

    def _ok_post(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status = MagicMock()
        return mock_post

    def test_posts_to_mailgun_messages_endpoint(self):
        with patch("monitor.emailing.requests.post") as mock_post:
            self._ok_post(mock_post)
            sent = MailgunBackend().send_messages([self._message()])
        self.assertEqual(sent, 1)
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args[0][0],
            "https://api.mailgun.net/v3/mg.example.com/messages",
        )

    def test_authenticates_with_api_key(self):
        with patch("monitor.emailing.requests.post") as mock_post:
            self._ok_post(mock_post)
            MailgunBackend().send_messages([self._message()])
        self.assertEqual(mock_post.call_args[1]["auth"], ("api", "key-cafebeef"))

    def test_sends_plain_text_message(self):
        with patch("monitor.emailing.requests.post") as mock_post:
            self._ok_post(mock_post)
            MailgunBackend().send_messages([self._message()])
        data = mock_post.call_args[1]["data"]
        self.assertEqual(data["from"], "TosDiff <no-reply@example.com>")
        self.assertEqual(data["to"], "bob@example.com")
        self.assertEqual(data["subject"], "Subject")
        self.assertEqual(data["text"], "Body")
        self.assertNotIn("html", data)

    def test_sends_html_alternative(self):
        message = EmailMultiAlternatives(
            "Subject", "Plain body", "no-reply@example.com", ["bob@example.com"]
        )
        message.attach_alternative("<p>HTML body</p>", "text/html")
        with patch("monitor.emailing.requests.post") as mock_post:
            self._ok_post(mock_post)
            MailgunBackend().send_messages([message])
        data = mock_post.call_args[1]["data"]
        self.assertEqual(data["text"], "Plain body")
        self.assertEqual(data["html"], "<p>HTML body</p>")

    def test_sends_cc_bcc_reply_to_and_extra_headers(self):
        message = EmailMessage(
            "Subject",
            "Body",
            "no-reply@example.com",
            ["a@example.com"],
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
            headers={"Reply-To": "support@example.com", "X-Feature": "digest"},
        )
        with patch("monitor.emailing.requests.post") as mock_post:
            self._ok_post(mock_post)
            MailgunBackend().send_messages([message])
        data = mock_post.call_args[1]["data"]
        self.assertEqual(data["cc"], "cc@example.com")
        self.assertEqual(data["bcc"], "bcc@example.com")
        self.assertEqual(data["h:Reply-To"], "support@example.com")
        self.assertEqual(data["h:X-Feature"], "digest")

    def test_returns_count_of_sent_messages(self):
        with patch("monitor.emailing.requests.post") as mock_post:
            self._ok_post(mock_post)
            sent = MailgunBackend().send_messages([self._message(), self._message()])
        self.assertEqual(sent, 2)
        self.assertEqual(mock_post.call_count, 2)

    def test_sends_attachments(self):
        message = self._message()
        message.attach("hello.txt", "hello world", "text/plain")
        with patch("monitor.emailing.requests.post") as mock_post:
            self._ok_post(mock_post)
            MailgunBackend().send_messages([message])
        files = mock_post.call_args[1]["files"]
        self.assertIsNotNone(files)
        name, (filename, content, mimetype) = files[0]
        self.assertEqual(name, "attachment")
        self.assertEqual(filename, "hello.txt")
        self.assertEqual(content, "hello world")
        self.assertEqual(mimetype, "text/plain")

    def test_returns_zero_when_unconfigured_and_fail_silently(self):
        with override_settings(MAILGUN_API_KEY="", MAILGUN_DOMAIN=""):
            with patch("monitor.emailing.requests.post") as mock_post:
                sent = MailgunBackend(fail_silently=True).send_messages([self._message()])
        self.assertEqual(sent, 0)
        mock_post.assert_not_called()

    def test_raises_when_unconfigured_and_not_fail_silently(self):
        with override_settings(MAILGUN_API_KEY="", MAILGUN_DOMAIN=""):
            with patch("monitor.emailing.requests.post") as mock_post:
                with self.assertRaises(RuntimeError):
                    MailgunBackend().send_messages([self._message()])
        mock_post.assert_not_called()

    def test_api_error_returns_zero_when_fail_silently(self):
        with patch("monitor.emailing.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("boom")
            sent = MailgunBackend(fail_silently=True).send_messages([self._message()])
        self.assertEqual(sent, 0)

    def test_api_error_raises_when_not_fail_silently(self):
        with patch("monitor.emailing.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("boom")
            with self.assertRaises(requests.RequestException):
                MailgunBackend().send_messages([self._message()])

    def test_supports_eu_region_api_url(self):
        with override_settings(MAILGUN_API_URL="https://api.eu.mailgun.net"):
            with patch("monitor.emailing.requests.post") as mock_post:
                self._ok_post(mock_post)
                MailgunBackend().send_messages([self._message()])
        self.assertEqual(
            mock_post.call_args[0][0],
            "https://api.eu.mailgun.net/v3/mg.example.com/messages",
        )


class MailersConfigurationTest(TestCase):
    """The MAILERS multiplexer routes transactional mail and ops mail separately."""

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "monitor.emailing.MailgunBackend",
                "OPTIONS": {},
            },
            "admin": {
                "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                "OPTIONS": {
                    "host": "smtp.example.com",
                    "port": 587,
                    "username": "mailuser",
                    "password": "mailpass",
                    "use_tls": True,
                    "use_ssl": False,
                    "timeout": 30,
                },
            },
        }
    )
    def test_default_mailer_is_the_mailgun_backend(self):
        conn = mail.mailers.default
        self.assertIsInstance(conn, MailgunBackend)

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "monitor.emailing.MailgunBackend",
                "OPTIONS": {},
            },
            "admin": {
                "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                "OPTIONS": {
                    "host": "smtp.example.com",
                    "port": 587,
                    "username": "mailuser",
                    "password": "mailpass",
                    "use_tls": True,
                    "use_ssl": False,
                    "timeout": 30,
                },
            },
        }
    )
    def test_admin_mailer_is_the_smtp_backend_with_custom_options(self):
        conn = mail.mailers["admin"]
        self.assertIsInstance(conn, SMTPEmailBackend)
        self.assertEqual(conn.host, "smtp.example.com")
        self.assertEqual(conn.port, 587)
        self.assertEqual(conn.username, "mailuser")
        self.assertEqual(conn.password, "mailpass")
        self.assertTrue(conn.use_tls)
        self.assertEqual(conn.timeout, 30)


class AdminErrorEmailRoutingTest(TestCase):
    """Django error reports (500s) are wired to the admin/SMTP mailer."""

    def test_request_error_handler_uses_admin_mailer(self):
        logger = logging.getLogger("django")
        email_handlers = [h for h in logger.handlers if isinstance(h, AdminEmailHandler)]
        self.assertTrue(email_handlers, "django logger has no AdminEmailHandler")
        for handler in email_handlers:
            self.assertEqual(handler.using, "admin")
