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
    ManageDashboardView,
    ManageDocumentCreateView,
    ManageDocumentListView,
    ManageDocumentUpdateView,
    ManageOrganizationCreateView,
    ManageOrganizationListView,
    ManageOrganizationUpdateView,
    ManageSuggestionListView,
    ManageSuggestionReviewView,
    ManageTagCreateView,
    ManageTagListView,
    ManageTagUpdateView,
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
    # Superuser management pages
    path("manage/", ManageDashboardView.as_view(), name="manage_dashboard"),
    path(
        "manage/organizations/", ManageOrganizationListView.as_view(), name="manage_organizations"
    ),
    path(
        "manage/organizations/add/",
        ManageOrganizationCreateView.as_view(),
        name="manage_organization_create",
    ),
    path(
        "manage/organizations/<int:pk>/",
        ManageOrganizationUpdateView.as_view(),
        name="manage_organization_update",
    ),
    path("manage/documents/", ManageDocumentListView.as_view(), name="manage_documents"),
    path(
        "manage/documents/add/", ManageDocumentCreateView.as_view(), name="manage_document_create"
    ),
    path(
        "manage/organizations/<int:organization_pk>/documents/add/",
        ManageDocumentCreateView.as_view(),
        name="manage_document_create_for_organization",
    ),
    path(
        "manage/documents/<int:pk>/",
        ManageDocumentUpdateView.as_view(),
        name="manage_document_update",
    ),
    path("manage/suggestions/", ManageSuggestionListView.as_view(), name="manage_suggestions"),
    path(
        "manage/suggestions/<int:pk>/approve/",
        ManageSuggestionReviewView.as_view(action="approve"),
        name="manage_suggestion_approve",
    ),
    path(
        "manage/suggestions/<int:pk>/reject/",
        ManageSuggestionReviewView.as_view(action="reject"),
        name="manage_suggestion_reject",
    ),
    path("manage/tags/", ManageTagListView.as_view(), name="manage_tags"),
    path("manage/tags/add/", ManageTagCreateView.as_view(), name="manage_tag_create"),
    path(
        "manage/tags/<int:pk>/",
        ManageTagUpdateView.as_view(),
        name="manage_tag_update",
    ),
]
