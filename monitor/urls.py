from django.urls import path

from .views import DocumentDetailView, RecentChangesView, SnapshotDiffView, SnapshotTextView

app_name = "monitor"

urlpatterns = [
    path("", RecentChangesView.as_view(), name="home"),
    path("document/<int:pk>/", DocumentDetailView.as_view(), name="document_detail"),
    path(
        "document/<int:pk>/diff/<int:old_pk>/<int:new_pk>/",
        SnapshotDiffView.as_view(),
        name="snapshot_diff",
    ),
    path(
        "document/<int:pk>/snapshot/<int:snap_pk>/",
        SnapshotTextView.as_view(),
        name="snapshot_text",
    ),
]
