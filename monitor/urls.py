from django.urls import path

from .views import AboutView, DocumentDetailView, OrganizationsView, PrivacyView, RecentChangesView, SnapshotDiffView, SnapshotTextView, TermsView

app_name = "monitor"

urlpatterns = [
    path("", RecentChangesView.as_view(), name="home"),
    path("organizations/", OrganizationsView.as_view(), name="organizations"),
    path("about/", AboutView.as_view(), name="about"),
    path("terms/", TermsView.as_view(), name="terms"),
    path("privacy/", PrivacyView.as_view(), name="privacy"),
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
