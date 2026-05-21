from django.db import models
from django.utils.text import slugify


class Country(models.Model):
    name = models.CharField(max_length=100, help_text='Display name, e.g. "United States", "France"')
    code = models.CharField(max_length=2, unique=True, help_text='ISO 3166-1 alpha-2 code, e.g. "US", "FR"')

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


class Organization(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    website_url = models.URLField(max_length=500)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subsidiaries",
        help_text="Parent organization, e.g. Meta is the parent of Facebook and Instagram.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


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

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="documents"
    )
    document_type = models.CharField(
        max_length=20, choices=DocumentType.choices, default=DocumentType.TERMS_OF_SERVICE
    )
    url = models.URLField(max_length=500)
    fetch_method = models.CharField(
        max_length=20, choices=FetchMethod.choices, default=FetchMethod.REQUESTS
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

    class Meta:
        ordering = ["organization", "document_type"]
        unique_together = [("organization", "document_type", "url")]

    def __str__(self) -> str:
        return f"{self.organization.name} — {self.get_document_type_display()}"


class DocumentSnapshot(models.Model):
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="snapshots"
    )
    captured_at = models.DateTimeField(auto_now_add=True)
    cleaned_text = models.TextField()
    text_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ["-captured_at"]

    def __str__(self) -> str:
        return f"{self.document} @ {self.captured_at:%Y-%m-%d %H:%M}"
