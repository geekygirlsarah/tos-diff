"""
Unit tests for the monitor app.

Run with:  python manage.py test monitor
"""

from unittest.mock import MagicMock, patch

import requests

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Country, Document, DocumentSnapshot, Language, Organization, Tag
from .services import (
    clean_html,
    compute_hash,
    create_snapshot_if_changed,
    extract_pdf_text,
    extract_text,
    fetch_and_snapshot,
    fetch_pdf_bytes,
)


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
        with self.assertRaises(Exception):
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
        facebook = Organization.objects.create(
            name="Facebook", website_url="https://facebook.com", parent=meta
        )
        instagram = Organization.objects.create(
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
        with self.assertRaises(Exception):
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
        with self.assertRaises(Exception):
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
        mock_get.return_value = self._make_response("application/octet-stream", "https://example.com/file.pdf")
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
        import io as _io
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
            document=self.doc, cleaned_text="Old line one\nOld line two", text_hash=compute_hash("old")
        )
        self.snap_new = DocumentSnapshot.objects.create(
            document=self.doc, cleaned_text="New line one\nNew line two", text_hash=compute_hash("new")
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


class OrganizationsViewTest(TestCase):
    def setUp(self):
        self.parent = Organization.objects.create(name="Meta", website_url="https://meta.com")
        self.child = Organization.objects.create(
            name="Instagram", website_url="https://instagram.com", parent=self.parent
        )
        self.doc = Document.objects.create(
            organization=self.parent, url="https://meta.com/tos"
        )
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
