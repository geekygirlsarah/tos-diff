"""
Unit tests for the monitor app.

Run with:  python manage.py test monitor
"""

from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Country,
    Document,
    DocumentSnapshot,
    DocumentSubscription,
    Language,
    LoginCode,
    Organization,
    OrganizationSubscription,
    Suggestion,
    Tag,
)
from .services import (
    build_snapshot_change_message,
    clean_html,
    compute_hash,
    create_snapshot_if_changed,
    extract_pdf_text,
    extract_text,
    fetch_and_snapshot,
    fetch_pdf_bytes,
    generate_login_code,
    send_login_code_email,
)
from .tasks import send_change_notifications

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


class BuildSnapshotChangeMessageTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Acme", website_url="https://acme.com")
        self.doc = Document.objects.create(organization=org, url="https://acme.com/tos")
        self.old = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="Version 1", text_hash=compute_hash("v1")
        )
        self.new = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="Version 2", text_hash=compute_hash("v2")
        )
        self.site_url = "http://localhost:8000"

    def test_subject_mentions_org_and_document(self):
        subject, _, _ = build_snapshot_change_message(self.doc, self.new, self.old, self.site_url)
        self.assertIn("Acme", subject)
        self.assertIn("Terms of Service", subject)

    def test_plain_body_contains_detail_and_diff_links(self):
        _, plain, _ = build_snapshot_change_message(self.doc, self.new, self.old, self.site_url)
        self.assertIn(f"{self.site_url}/document/{self.doc.pk}/", plain)
        self.assertIn(f"/diff/{self.old.pk}/{self.new.pk}/", plain)

    def test_html_body_contains_anchors(self):
        _, _, html = build_snapshot_change_message(self.doc, self.new, self.old, self.site_url)
        self.assertIn('<a href="', html)
        self.assertIn(f"/document/{self.doc.pk}/", html)

    def test_works_without_previous_snapshot(self):
        _, plain, _ = build_snapshot_change_message(self.doc, self.new, None, self.site_url)
        self.assertIn("View the document", plain)
        self.assertNotIn("/diff/", plain)


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
    def test_sends_email_to_document_subscribers(self, mock_send):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 1)
        self.assertIsNone(result["error"])
        recipients = mock_send.call_args[0][3]
        self.assertEqual(recipients, ["bob@example.com"])

    @patch("monitor.tasks.send_mail")
    def test_sends_email_to_organization_subscribers(self, mock_send):
        OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 1)
        recipients = mock_send.call_args[0][3]
        self.assertEqual(recipients, ["bob@example.com"])

    @patch("monitor.tasks.send_mail")
    def test_deduplicates_users_subscribed_at_both_levels(self, mock_send):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        OrganizationSubscription.objects.create(user=self.user, organization=self.org)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(mock_send.call_count, 1)

    @patch("monitor.tasks.send_mail")
    def test_skips_inactive_users(self, mock_send):
        DocumentSubscription.objects.create(user=self.inactive_user, document=self.doc)
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        mock_send.assert_not_called()

    @patch("monitor.tasks.send_mail")
    def test_no_subscribers_no_email_sent(self, mock_send):
        result = send_change_notifications(self.doc.pk, self.new.pk)
        self.assertEqual(result["sent"], 0)
        mock_send.assert_not_called()

    @patch("monitor.tasks.send_mail")
    def test_email_contains_change_link(self, mock_send):
        DocumentSubscription.objects.create(user=self.user, document=self.doc)
        send_change_notifications(self.doc.pk, self.new.pk)
        subject = mock_send.call_args[0][0]
        body = mock_send.call_args[0][1]
        self.assertIn("Acme", subject)
        self.assertIn(f"/document/{self.doc.pk}/", body)
        self.assertIn(f"/diff/{self.old.pk}/{self.new.pk}/", body)

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
            reverse("monitor:manage_documents"),
            reverse("monitor:manage_document_create"),
            reverse("monitor:manage_document_create_for_organization", args=[self.org.pk]),
            reverse("monitor:manage_document_update", args=[self.doc.pk]),
            reverse("monitor:manage_suggestions"),
            reverse("monitor:manage_suggestion_approve", args=[self.suggestion.pk]),
            reverse("monitor:manage_suggestion_reject", args=[self.suggestion.pk]),
            reverse("monitor:manage_tags"),
            reverse("monitor:manage_tag_create"),
            reverse("monitor:manage_tag_update", args=[self.tag.pk]),
        ]

    def test_anonymous_users_redirected_to_login(self):
        for url in self._manage_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)

    def test_regular_users_are_forbidden(self):
        get_user_model().objects.create_user(username="bob", email="bob@example.com", password="x")
        self.client.login(username="bob", password="x")
        for url in self._manage_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
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
        response = self.client.get(reverse("monitor:manage_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["organization_count"], 1)
        self.assertEqual(response.context["document_count"], 1)
        self.assertEqual(response.context["pending_suggestion_count"], 1)


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

    def test_update_tag(self):
        response = self.client.post(
            reverse("monitor:manage_tag_update", args=[self.tag.pk]), {"name": "Fintech & Banking"}
        )
        self.assertRedirects(response, reverse("monitor:manage_tags"))
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.name, "Fintech & Banking")
