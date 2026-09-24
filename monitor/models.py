from urllib.parse import urlparse

from django.conf import settings
from django.db import models
from django.utils.text import slugify


class Country(models.Model):
    name = models.CharField(
        max_length=100, help_text='Display name, e.g. "United States", "France"'
    )
    code = models.CharField(
        max_length=2, unique=True, help_text='ISO 3166-1 alpha-2 code, e.g. "US", "FR"'
    )

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "countries"

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class Language(models.Model):
    name = models.CharField(max_length=100, help_text='Display name, e.g. "English", "Français"')
    code = models.CharField(max_length=10, unique=True, help_text='ISO 639-1 code, e.g. "en", "fr"')

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class Tag(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Organization(models.Model):
    class Category(models.TextChoices):
        TECHNOLOGY = "technology", "Technology & Software"
        FINANCIAL = "financial", "Financial Services"
        HEALTHCARE = "healthcare", "Healthcare & Pharmaceuticals"
        ENTERTAINMENT_STREAMING = "entertainment_streaming", "Entertainment & Streaming"
        MEDIA = "media", "Media & Entertainment"
        SOCIAL_MEDIA = "social_media", "Social Media & Content Platforms"
        HOSPITALITY = "hospitality", "Hospitality & Hotels"
        RETAIL = "retail", "Retail & E-Commerce"
        OTHER = "other", "Other"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    website_url = models.URLField(max_length=500)
    category = models.CharField(
        max_length=30,
        choices=Category.choices,
        blank=True,
        default="",
        help_text="Industry category for this organization.",
    )
    tags = models.ManyToManyField(
        Tag,
        blank=True,
        related_name="organizations",
        help_text="Tags for this organization.",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subsidiaries",
        help_text="Parent organization, e.g. Meta is the parent of Facebook and Instagram.",
    )
    is_failing = models.BooleanField(
        default=False,
        help_text="Set to True if this organization's documents are consistently failing to fetch.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def favicon_url(self) -> str:
        """DuckDuckGo favicon service URL for this organization's website domain."""
        hostname = urlparse(self.website_url).hostname or ""
        if not hostname:
            return ""
        return f"https://icons.duckduckgo.com/ip3/{hostname}.ico"


class Document(models.Model):
    class DocumentType(models.TextChoices):
        TERMS_OF_SERVICE = "tos", "Terms of Service"
        PRIVACY_POLICY = "privacy", "Privacy Policy"
        COOKIE_POLICY = "cookie", "Cookie Policy"
        REFUND_POLICY = "refund", "Refund Policy"
        CHILDRENS_PRIVACY = "childrens_privacy", "Children's Privacy Policy"
        SUBSCRIPTION_POLICY = "subscription", "Subscription Policy"
        SERVICE_AGREEMENT = "service_agreement", "Service Agreement"
        SERVICE_FEES = "service_fees", "Service Fees"
        USER_AGREEMENT = "user_agreement", "User Agreement"
        CODE_OF_CONDUCT = "conduct", "Code of Conduct / Community Standards"
        ACCEPTABLE_USE = "acceptable_use", "Acceptable Use Policy"
        DMCA_POLICY = "dmca", "DMCA / Copyright Policy"
        PAYMENT_SERVICE_TERMS = "payment_service", "Payment Service Terms"
        OTHER = "other", "Other"

    class FetchMethod(models.TextChoices):
        REQUESTS = "requests", "HTTP Requests"
        PLAYWRIGHT = "playwright", "Playwright (headless browser)"

    class DocumentFormat(models.TextChoices):
        HTML = "html", "HTML"
        PDF = "pdf", "PDF"
        TXT = "txt", "TXT"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="documents"
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Custom name for this document. If blank, the category will be used.",
    )
    document_type = models.CharField(
        max_length=20, choices=DocumentType.choices, default=DocumentType.TERMS_OF_SERVICE
    )
    url = models.URLField(max_length=500)
    fetch_method = models.CharField(
        max_length=20, choices=FetchMethod.choices, default=FetchMethod.REQUESTS
    )
    document_format = models.CharField(
        max_length=10,
        choices=DocumentFormat.choices,
        default=DocumentFormat.HTML,
        help_text="Format of the fetched document (set automatically during fetch).",
    )
    language = models.ForeignKey(
        Language,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        help_text="Language this document is written in.",
    )
    country = models.ForeignKey(
        Country,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Country this document specifically applies to, if any.",
    )
    last_checked = models.DateTimeField(null=True, blank=True)
    last_changed = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_failing = models.BooleanField(
        default=False,
        help_text="Set to True if this document is consistently failing to fetch.",
    )
    other_document_type = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text='Describe the document type when "Other" is selected.',
    )
    custom_selectors = models.TextField(
        blank=True,
        default="",
        help_text=(
            "CSS selectors to exclude before text extraction, one per line. "
            "Example: .cookie-banner\n#sidebar"
        ),
    )
    fetch_config = models.JSONField(
        blank=True,
        null=True,
        default=None,
        help_text=(
            "Optional Playwright fetch configuration. "
            "Keys: wait_for_selector (CSS selector to wait for), "
            "sleep_seconds (extra wait time for JS rendering), "
            "dismiss_selectors (list of CSS selectors for cookie/modal buttons to click)."
        ),
    )

    class Meta:
        ordering = ["organization", "document_type"]
        unique_together = [("organization", "document_type", "url")]

    @property
    def display_name(self) -> str:
        """Returns the custom name if set, otherwise falls back to document type/other type."""
        if self.name:
            return self.name
        if self.document_type == self.DocumentType.OTHER and self.other_document_type:
            return self.other_document_type
        return self.get_document_type_display()

    def __str__(self) -> str:
        return f"{self.organization.name} — {self.display_name}"


class DocumentSnapshot(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="snapshots")
    captured_at = models.DateTimeField(auto_now_add=True)
    cleaned_text = models.TextField()
    text_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ["-captured_at"]

    def __str__(self) -> str:
        return f"{self.document} @ {self.captured_at:%Y-%m-%d %H:%M}"


class Suggestion(models.Model):
    """User-submitted request to add a new website / policy document to track."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    organization_name = models.CharField(
        max_length=255, help_text="Suggested company or organization name."
    )
    website_url = models.URLField(max_length=500, help_text="Company home page / website URL.")
    document_url = models.URLField(max_length=500, help_text="Direct URL to the policy document.")
    document_type = models.CharField(
        max_length=20,
        choices=Document.DocumentType.choices,
        default=Document.DocumentType.TERMS_OF_SERVICE,
        help_text="Type of document being suggested.",
    )
    other_document_type = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text='Describe the document type when "Other" is selected.',
    )
    contact_email = models.EmailField(
        blank=True,
        default="",
        help_text="Optional email address if we need to follow up.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suggestions",
        help_text="The logged-in account that submitted this suggestion, if any.",
    )
    notes = models.TextField(
        blank=True, default="", help_text="Any additional notes about the suggestion."
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Review status of this suggestion.",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(
        null=True, blank=True, help_text="When the suggestion was reviewed."
    )
    review_notes = models.TextField(
        blank=True, default="", help_text="Internal notes from the review process."
    )

    class Meta:
        ordering = ["-submitted_at", "-pk"]

    def __str__(self) -> str:
        return self.organization_name

    def create_organization_and_document(self) -> tuple["Organization", "Document"]:
        """
        Create (or fetch) an Organization and Document from this suggestion.

        Idempotent: calling it more than once will not create duplicates.
        """
        organization, _ = Organization.objects.get_or_create(
            website_url=self.website_url,
            defaults={"name": self.organization_name},
        )
        document, _ = Document.objects.get_or_create(
            organization=organization,
            document_type=self.document_type,
            url=self.document_url,
            defaults={"other_document_type": self.other_document_type},
        )
        return organization, document


class LoginCode(models.Model):
    """One-time login codes emailed to users instead of passwords."""

    email = models.EmailField(db_index=True)
    code_hash = models.CharField(max_length=64, help_text="SHA-256 of the plain code.")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.email} ({'used' if self.used_at else 'unused'})"


class DocumentSubscription(models.Model):
    """A user follows a specific document and gets notified on changes."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="document_subscriptions",
    )
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="subscribers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "document")]
        ordering = ["document__organization__name", "document__document_type"]

    def __str__(self) -> str:
        return f"{self.user} → {self.document}"


class OrganizationSubscription(models.Model):
    """A user follows an organization and gets notified when any of its docs change."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_subscriptions",
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="subscribers"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "organization")]
        ordering = ["organization__name"]

    def __str__(self) -> str:
        return f"{self.user} → {self.organization}"


class NotificationPreference(models.Model):
    """How often a user wants change notification emails."""

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily digest"
        WEEKLY = "weekly", "Weekly digest"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    frequency = models.CharField(
        max_length=10,
        choices=Frequency.choices,
        default=Frequency.DAILY,
        help_text="Batch change emails into a daily or weekly digest.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.user} — {self.get_frequency_display()}"


class PendingNotification(models.Model):
    """A change notification queued for a user on the daily/weekly digest."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_notifications",
    )
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="+")
    snapshot = models.ForeignKey(DocumentSnapshot, on_delete=models.CASCADE, related_name="+")
    old_snapshot = models.ForeignKey(
        DocumentSnapshot,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.user} → {self.document} ({self.created_at:%Y-%m-%d %H:%M})"
