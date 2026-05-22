import difflib

from django.http import Http404
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView

from .models import Document, DocumentSnapshot, Organization


VALID_DAYS = (3, 7, 14, 30)


class RecentChangesView(ListView):
    template_name = "monitor/home.html"
    context_object_name = "snapshots"
    paginate_by = 20

    def _get_days(self) -> int:
        try:
            days = int(self.request.GET.get("days", 14))
        except (ValueError, TypeError):
            days = 14
        return days if days in VALID_DAYS else 14

    def get_queryset(self):
        days = self._get_days()
        cutoff = timezone.now() - timezone.timedelta(days=days)
        return (
            DocumentSnapshot.objects.filter(captured_at__gte=cutoff)
            .select_related("document__organization")
            .order_by("-captured_at")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        days = self._get_days()
        context["days"] = days
        context["valid_days"] = VALID_DAYS
        context["page_title"] = f"Recent Changes (last {days} days)"
        return context


class DocumentDetailView(DetailView):
    model = Document
    template_name = "monitor/document_detail.html"
    context_object_name = "document"

    def get_queryset(self):
        return Document.objects.select_related("organization")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        snapshots = (
            self.object.snapshots.order_by("-captured_at")
        )
        context["snapshots"] = snapshots
        # Pair each snapshot with the one before it (older) for diff links
        snapshot_list = list(snapshots)
        pairs = []
        for i, snap in enumerate(snapshot_list):
            older = snapshot_list[i + 1] if i + 1 < len(snapshot_list) else None
            pairs.append((snap, older))
        context["snapshot_pairs"] = pairs
        return context


class SnapshotDiffView(DetailView):
    """Side-by-side diff between two snapshots of the same document."""
    model = Document
    template_name = "monitor/snapshot_diff.html"
    context_object_name = "document"

    def get_queryset(self):
        return Document.objects.select_related("organization")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        doc = self.object

        try:
            snap_new = doc.snapshots.get(pk=self.kwargs["new_pk"])
            snap_old = doc.snapshots.get(pk=self.kwargs["old_pk"])
        except DocumentSnapshot.DoesNotExist:
            raise Http404("Snapshot not found.")

        old_lines = snap_old.cleaned_text.splitlines()
        new_lines = snap_new.cleaned_text.splitlines()

        diff_html = difflib.HtmlDiff(wrapcolumn=80).make_table(
            old_lines,
            new_lines,
            fromdesc=f"Snapshot {snap_old.captured_at:%Y-%m-%d %H:%M UTC}",
            todesc=f"Snapshot {snap_new.captured_at:%Y-%m-%d %H:%M UTC}",
            context=True,
            numlines=3,
        )

        context["snap_old"] = snap_old
        context["snap_new"] = snap_new
        context["diff_html"] = diff_html
        return context


class OrganizationsView(ListView):
    """Lists all organizations with their documents."""
    template_name = "monitor/organizations.html"
    context_object_name = "organizations"

    def get_queryset(self):
        from django.db.models import Count, Q
        return (
            Organization.objects.filter(parent__isnull=True)
            .annotate(
                own_doc_count=Count("documents", distinct=True),
                sub_doc_count=Count("subsidiaries__documents", distinct=True),
            )
            .filter(Q(own_doc_count__gt=0) | Q(sub_doc_count__gt=0))
            .prefetch_related(
                "documents",
                "subsidiaries",
                "subsidiaries__documents",
            )
            .order_by("name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Organizations"
        return context


class AboutView(TemplateView):
    """Static about page."""
    template_name = "monitor/about.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "About"
        return context


class TermsView(TemplateView):
    """Terms of Use page."""
    template_name = "monitor/terms.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Terms of Use"
        return context


class PrivacyView(TemplateView):
    """Privacy Policy page."""
    template_name = "monitor/privacy.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Privacy Policy"
        return context


class SnapshotTextView(DetailView):
    """Full plain-text view of a single snapshot."""
    model = DocumentSnapshot
    template_name = "monitor/snapshot_text.html"
    context_object_name = "snapshot"
    pk_url_kwarg = "snap_pk"

    def get_queryset(self):
        return DocumentSnapshot.objects.select_related("document__organization")

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        # Ensure the snapshot belongs to the document in the URL
        if obj.document_id != self.kwargs["pk"]:
            raise Http404("Snapshot does not belong to this document.")
        return obj
