import difflib

from django.http import Http404
from django.utils import timezone
from django.views.generic import DetailView, ListView

from .models import Document, DocumentSnapshot


class RecentChangesView(ListView):
    template_name = "monitor/home.html"
    context_object_name = "snapshots"
    paginate_by = 20

    def get_queryset(self):
        cutoff = timezone.now() - timezone.timedelta(days=14)
        return (
            DocumentSnapshot.objects.filter(captured_at__gte=cutoff)
            .select_related("document__organization")
            .order_by("-captured_at")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Recent Changes (last 14 days)"
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
