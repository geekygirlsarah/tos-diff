from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import (
    AboutView,
    AccountView,
    DocumentDetailView,
    DocumentSubscribeView,
    DocumentUnsubscribeView,
    LoginRequestView,
    LoginVerifyView,
    OrganizationSubscribeView,
    OrganizationsView,
    OrganizationUnsubscribeView,
    PrivacyView,
    RecentChangesView,
    SnapshotDiffView,
    SnapshotTextView,
    SuggestDocumentView,
    SuggestionThanksView,
    TermsView,
)

app_name = "monitor"

urlpatterns = [
    path("", RecentChangesView.as_view(), name="home"),
    path("organizations/", OrganizationsView.as_view(), name="organizations"),
    path("about/", AboutView.as_view(), name="about"),
    path("terms/", TermsView.as_view(), name="terms"),
    path("privacy/", PrivacyView.as_view(), name="privacy"),
    path("suggest/", SuggestDocumentView.as_view(), name="suggest"),
    path("suggest/thanks/", SuggestionThanksView.as_view(), name="suggestion_thanks"),
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
    path(
        "document/<int:pk>/subscribe/",
        DocumentSubscribeView.as_view(),
        name="document_subscribe",
    ),
    path(
        "document/<int:pk>/unsubscribe/",
        DocumentUnsubscribeView.as_view(),
        name="document_unsubscribe",
    ),
    path(
        "organization/<int:pk>/subscribe/",
        OrganizationSubscribeView.as_view(),
        name="organization_subscribe",
    ),
    path(
        "organization/<int:pk>/unsubscribe/",
        OrganizationUnsubscribeView.as_view(),
        name="organization_unsubscribe",
    ),
    path("accounts/login/", LoginRequestView.as_view(), name="login_request"),
    path("accounts/verify/", LoginVerifyView.as_view(), name="login_verify"),
    path(
        "accounts/logout/",
        LogoutView.as_view(next_page="monitor:home"),
        name="logout",
    ),
    path("accounts/", AccountView.as_view(), name="account"),
]
