from django.contrib import admin
from django.utils.html import format_html

from .models import Country, Document, DocumentSnapshot, Language, Organization, Tag


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
    list_display = ["name", "slug", "category", "parent", "website_url_link", "document_count"]
    list_filter = ["category", "tags"]
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
        "document_type",
        "language",
        "country",
        "fetch_method",
        "url_link",
        "last_checked",
        "last_changed",
        "is_active",
    ]
    list_filter = ["document_type", "language", "country", "fetch_method", "is_active", "organization"]
    search_fields = ["organization__name", "url"]
    list_select_related = ["organization"]
    readonly_fields = ["last_checked", "last_changed"]
    fieldsets = [
        (None, {"fields": ["organization", "document_type", "other_document_type", "language", "country", "url", "fetch_method", "is_active"]}),
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
