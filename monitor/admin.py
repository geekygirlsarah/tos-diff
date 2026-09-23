from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from django.utils.html import format_html

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


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ["name", "code"]
    search_fields = ["name", "code"]


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ["name", "code"]
    search_fields = ["name", "code"]


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "slug",
        "category",
        "parent",
        "is_failing",
        "website_url_link",
        "document_count",
    ]
    list_filter = ["category", "is_failing", "tags"]
    search_fields = ["name", "slug", "website_url"]
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ["parent", "tags"]

    @admin.display(description="Website")
    def website_url_link(self, obj: Organization) -> str:
        return format_html('<a href="{}" target="_blank">{}</a>', obj.website_url, obj.website_url)

    @admin.display(description="Documents")
    def document_count(self, obj: Organization) -> int:
        return obj.documents.count()


class DocumentSnapshotInline(admin.TabularInline):
    model = DocumentSnapshot
    extra = 0
    readonly_fields = ["captured_at", "text_hash", "cleaned_text_preview"]
    fields = ["captured_at", "text_hash", "cleaned_text_preview"]
    can_delete = False

    @admin.display(description="Preview")
    def cleaned_text_preview(self, obj: DocumentSnapshot) -> str:
        return obj.cleaned_text[:200] + "…" if len(obj.cleaned_text) > 200 else obj.cleaned_text


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = [
        "organization",
        "display_name",
        "document_type",
        "language",
        "country",
        "fetch_method",
        "url_link",
        "is_active",
        "is_failing",
    ]
    list_filter = [
        "document_type",
        "language",
        "country",
        "fetch_method",
        "is_active",
        "is_failing",
        "organization",
    ]
    search_fields = ["organization__name", "name", "url"]
    list_select_related = ["organization"]
    readonly_fields = ["last_checked", "last_changed"]
    fieldsets = [
        (
            None,
            {
                "fields": [
                    "organization",
                    "name",
                    "document_type",
                    "other_document_type",
                    "language",
                    "country",
                    "url",
                    "fetch_method",
                    "is_active",
                    "is_failing",
                ]
            },
        ),
        ("Timestamps", {"fields": ["last_checked", "last_changed"], "classes": ["collapse"]}),
        (
            "Extraction settings",
            {
                "fields": ["custom_selectors"],
                "description": "CSS selectors to strip before text extraction (one per line).",
            },
        ),
    ]
    inlines = [DocumentSnapshotInline]

    @admin.display(description="URL")
    def url_link(self, obj: Document) -> str:
        return format_html('<a href="{}" target="_blank">{}</a>', obj.url, obj.url[:60])


@admin.register(DocumentSnapshot)
class DocumentSnapshotAdmin(admin.ModelAdmin):
    list_display = ["document", "captured_at", "text_hash"]
    list_filter = ["document__organization", "document__document_type"]
    search_fields = ["document__organization__name", "text_hash"]
    readonly_fields = ["document", "captured_at", "text_hash", "cleaned_text"]
    list_select_related = ["document__organization"]


@admin.register(Suggestion)
class SuggestionAdmin(admin.ModelAdmin):
    list_display = [
        "organization_name",
        "document_type",
        "status",
        "contact_email",
        "submitted_at",
        "reviewed_at",
    ]
    list_filter = ["status", "document_type"]
    search_fields = ["organization_name", "website_url", "document_url", "contact_email"]
    readonly_fields = ["submitted_at"]
    actions = ["approve_and_create", "approve", "reject"]

    @admin.action(description="Approve and create Organization + Document")
    def approve_and_create(self, request: HttpRequest, queryset: QuerySet) -> None:
        now = timezone.now()
        for suggestion in queryset.filter(status=Suggestion.Status.PENDING):
            suggestion.create_organization_and_document()
            suggestion.status = Suggestion.Status.APPROVED
            suggestion.reviewed_at = now
            suggestion.save(update_fields=["status", "reviewed_at"])
        self.message_user(request, "Approved selected suggestions and created orgs/documents.")

    @admin.action(description="Approve (status only)")
    def approve(self, request: HttpRequest, queryset: QuerySet) -> None:
        queryset.filter(status=Suggestion.Status.PENDING).update(
            status=Suggestion.Status.APPROVED,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, "Marked suggestions as approved.")

    @admin.action(description="Reject")
    def reject(self, request: HttpRequest, queryset: QuerySet) -> None:
        queryset.filter(status=Suggestion.Status.PENDING).update(
            status=Suggestion.Status.REJECTED,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, "Marked suggestions as rejected.")


@admin.register(LoginCode)
class LoginCodeAdmin(admin.ModelAdmin):
    list_display = ["email", "code_hash", "created_at", "expires_at", "used_at"]
    list_filter = ["used_at"]
    search_fields = ["email"]
    readonly_fields = ["email", "code_hash", "created_at", "expires_at", "used_at"]


@admin.register(DocumentSubscription)
class DocumentSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["user", "document", "created_at"]
    search_fields = ["user__username", "user__email", "document__organization__name"]
    list_select_related = ["user", "document__organization"]
    raw_id_fields = ["user", "document"]


@admin.register(OrganizationSubscription)
class OrganizationSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "created_at"]
    search_fields = ["user__username", "user__email", "organization__name"]
    list_select_related = ["user", "organization"]
    raw_id_fields = ["user", "organization"]
