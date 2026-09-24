from __future__ import annotations

import difflib
import secrets
from itertools import groupby
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import LogoutView  # noqa: F401 – re-exported for urls
from django.core import signing
from django.db.models import Count, Exists, OuterRef, Q
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView
from django.views.generic.edit import CreateView, DeleteView, FormView, UpdateView

from .forms import (
    CodeLoginForm,
    CountryForm,
    DocumentForm,
    EmailLoginForm,
    LanguageForm,
    NotificationPreferenceForm,
    OrganizationForm,
    SuggestionForm,
    SuggestionReviewForm,
    TagForm,
)
from .models import (
    Country,
    Document,
    DocumentSnapshot,
    DocumentSubscription,
    Language,
    LoginCode,
    NotificationPreference,
    Organization,
    OrganizationSubscription,
    Suggestion,
    Tag,
)
from .services import (
    LOGIN_CODE_TTL_MINUTES,
    compute_hash,
    generate_login_code,
    read_unsubscribe_token,
    send_login_code_email,
    send_suggestion_review_email,
)
from .tasks import check_document

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


VALID_DAYS = (3, 7, 14, 30)


def _tracked_organizations():
    """Top-level organizations with at least one document (directly or via a subsidiary)."""
    return (
        Organization.objects.filter(parent__isnull=True)
        .annotate(
            own_doc_count=Count("documents", distinct=True),
            sub_doc_count=Count("subsidiaries__documents", distinct=True),
        )
        .filter(Q(own_doc_count__gt=0) | Q(sub_doc_count__gt=0))
    )


class RecentChangesView(ListView):
    template_name = "monitor/home.html"
    context_object_name = "day_groups"

    def _get_days(self) -> int:
        try:
            days = int(self.request.GET.get("days", 14))
        except (ValueError, TypeError):
            days = 14
        return days if days in VALID_DAYS else 14

    def _get_doc_type(self) -> str:
        doc_type = self.request.GET.get("type", "")
        valid_types = {choice[0] for choice in Document.DocumentType.choices}
        return doc_type if doc_type in valid_types else ""

    def _base_queryset(self):
        days = self._get_days()
        cutoff = timezone.now() - timezone.timedelta(days=days)
        return DocumentSnapshot.objects.filter(captured_at__gte=cutoff).select_related(
            "document__organization"
        )

    def get_queryset(self):
        qs = self._base_queryset()
        doc_type = self._get_doc_type()
        if doc_type:
            qs = qs.filter(document__document_type=doc_type)
        # Order by day (newest first), then organization (A–Z), then most recent first.
        return qs.order_by("-captured_at__date", "document__organization__name", "-captured_at")

    def _group_snapshots(self, snapshots):
        """Return [{date, organizations: [{organization, snapshots}]}] groups."""
        day_groups = []
        for day, day_snaps in groupby(snapshots, key=lambda s: s.captured_at.date()):
            organizations = []
            for _, org_snaps in groupby(day_snaps, key=lambda s: s.document.organization_id):
                org_snaps = list(org_snaps)
                organizations.append(
                    {
                        "organization": org_snaps[0].document.organization,
                        "snapshots": org_snaps,
                    }
                )
            day_groups.append({"date": day, "organizations": organizations})
        return day_groups

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        days = self._get_days()
        doc_type = self._get_doc_type()
        context["days"] = days
        context["valid_days"] = VALID_DAYS
        context["doc_type"] = doc_type
        context["page_title"] = f"Recent Changes (last {days} days)"
        context["total_organizations"] = _tracked_organizations().count()
        context["total_documents"] = Document.objects.count()

        # Group the (already ordered) snapshots by day, then organization.
        context["day_groups"] = self._group_snapshots(list(context["day_groups"]))

        # Distinct document types present in the current time-window
        type_values = (
            self._base_queryset()
            .values_list("document__document_type", flat=True)
            .distinct()
            .order_by("document__document_type")
        )
        context["document_types"] = [
            {"value": tv, "label": Document.DocumentType(tv).label} for tv in type_values
        ]
        return context


class DocumentDetailView(DetailView):
    model = Document
    template_name = "monitor/document_detail.html"
    context_object_name = "document"

    def get_queryset(self):
        return Document.objects.select_related("organization")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        snapshots = self.object.snapshots.order_by("-captured_at")
        context["snapshots"] = snapshots
        # Pair each snapshot with the one before it (older) for diff links
        snapshot_list = list(snapshots)
        pairs = []
        for i, snap in enumerate(snapshot_list):
            older = snapshot_list[i + 1] if i + 1 < len(snapshot_list) else None
            pairs.append((snap, older))
        context["snapshot_pairs"] = pairs

        user = self.request.user
        context["is_subscribed"] = (
            user.is_authenticated
            and DocumentSubscription.objects.filter(user=user, document=self.object).exists()
        )
        return context


class SnapshotDiffView(DetailView):
    """Side-by-side diff between two snapshots of the same document."""

    model = Document
    template_name = "monitor/snapshot_diff.html"
    context_object_name = "document"

    def get_queryset(self):
        return Document.objects.select_related("organization")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        doc = self.object

        try:
            snap_new = doc.snapshots.get(pk=self.kwargs["new_pk"])
            snap_old = doc.snapshots.get(pk=self.kwargs["old_pk"])
        except DocumentSnapshot.DoesNotExist:
            raise Http404("Snapshot not found.") from None

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
        return (
            _tracked_organizations()
            .prefetch_related(
                "documents",
                "subsidiaries",
                "subsidiaries__documents",
            )
            .order_by("name")
        )

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Organizations"
        context["total_organizations"] = len(context["organizations"])
        context["total_documents"] = Document.objects.count()

        user = self.request.user
        if user.is_authenticated:
            context["subscribed_org_ids"] = set(
                OrganizationSubscription.objects.filter(user=user).values_list(
                    "organization_id", flat=True
                )
            )
        else:
            context["subscribed_org_ids"] = set()

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


# ---------------------------------------------------------------------------
# OTP helpers
# ---------------------------------------------------------------------------


def _get_or_create_user_by_email(email: str) -> AbstractBaseUser:
    """Return the active user for *email*, creating an account on first login."""
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is not None:
        return user
    base = email.split("@")[0][:140] or "user"
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base[:130]}{suffix}"
    return User.objects.create(username=username, email=email)


def verify_login_code(email: str, code: str) -> AbstractBaseUser | None:
    """Validate *code* for *email*; return the authenticated user or None."""
    now = timezone.now()
    latest = (
        LoginCode.objects.filter(email=email, used_at__isnull=True, expires_at__gt=now)
        .order_by("-created_at")
        .first()
    )
    if latest is None:
        return None
    if not secrets.compare_digest(latest.code_hash, compute_hash(code.strip())):
        return None
    LoginCode.objects.filter(pk=latest.pk).update(used_at=now)
    return _get_or_create_user_by_email(email)


# ---------------------------------------------------------------------------
# Website suggestion views
# ---------------------------------------------------------------------------


class SuggestDocumentView(FormView):
    """Public form for suggesting a new company / policy document to track."""

    template_name = "monitor/suggest_document.html"
    form_class = SuggestionForm
    success_url = reverse_lazy("monitor:suggestion_thanks")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Suggest a website"
        return context

    def form_valid(self, form) -> HttpResponse:
        suggestion = form.save(commit=False)
        if self.request.user.is_authenticated:
            suggestion.user = self.request.user
        suggestion.save()
        return super().form_valid(form)


class SuggestionThanksView(TemplateView):
    """Confirmation page shown after submitting a suggestion."""

    template_name = "monitor/suggestion_thanks.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Thanks for your suggestion"
        return context


# ---------------------------------------------------------------------------
# OTP authentication views
# ---------------------------------------------------------------------------


class LoginRequestView(FormView):
    """Step 1: collect an email address and email the user a one-time code."""

    template_name = "monitor/login_request.html"
    form_class = EmailLoginForm

    def dispatch(self, request, *args, **kwargs) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("monitor:account")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Log in"
        return context

    def form_valid(self, form) -> HttpResponse:
        email = form.cleaned_data["email"]
        # Invalidate any outstanding codes before issuing a fresh one.
        LoginCode.objects.filter(email=email, used_at__isnull=True).update(used_at=timezone.now())
        code = generate_login_code()
        LoginCode.objects.create(
            email=email,
            code_hash=compute_hash(code),
            expires_at=timezone.now() + timezone.timedelta(minutes=LOGIN_CODE_TTL_MINUTES),
        )
        send_login_code_email(email, code)
        self.request.session["login_email"] = email
        self.request.session["login_next"] = self.request.GET.get("next", "")
        return redirect("monitor:login_verify")


class LoginVerifyView(FormView):
    """Step 2: verify the emailed one-time code and log the user in."""

    template_name = "monitor/login_verify.html"
    form_class = CodeLoginForm

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["email"] = self.request.session.get("login_email", "")
        context["page_title"] = "Enter your login code"
        return context

    def form_valid(self, form) -> HttpResponse:
        email = self.request.session.get("login_email", "")
        if not email:
            return redirect("monitor:login_request")

        user = verify_login_code(email, form.cleaned_data["code"])
        if user is None:
            form.add_error("code", "That code is invalid or has expired.")
            return self.form_invalid(form)

        login(self.request, user)
        self.request.session.pop("login_email", None)
        next_url = self.request.session.pop("login_next", "")
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={self.request.get_host()}
        ):
            return redirect(next_url)
        return redirect("monitor:account")


# ---------------------------------------------------------------------------
# Account & subscription views
# ---------------------------------------------------------------------------


class AccountView(LoginRequiredMixin, TemplateView):
    """Signed-in user's dashboard: subscriptions, preferences, and suggestions."""

    template_name = "monitor/account.html"
    login_url = "/accounts/login/"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        preference, _ = NotificationPreference.objects.get_or_create(user=user)
        context["page_title"] = "Your account"
        context["notification_preference_form"] = NotificationPreferenceForm(instance=preference)
        context["document_subscriptions"] = (
            DocumentSubscription.objects.filter(user=user)
            .select_related("document__organization")
            .order_by("document__organization__name", "document__document_type")
        )
        context["organization_subscriptions"] = (
            OrganizationSubscription.objects.filter(user=user)
            .select_related("organization")
            .order_by("organization__name")
        )
        context["my_suggestions"] = user.suggestions.order_by("-submitted_at", "-pk")[:10]
        return context

    def post(self, request, *args, **kwargs) -> HttpResponse:
        form = NotificationPreferenceForm(request.POST)
        if form.is_valid():
            preference, _ = NotificationPreference.objects.get_or_create(user=request.user)
            preference.frequency = form.cleaned_data["frequency"]
            preference.save(update_fields=["frequency", "updated_at"])
            messages.success(request, "Notification preferences updated.")
        return redirect("monitor:account")


class DocumentSubscribeView(LoginRequiredMixin, View):
    """POST to subscribe the signed-in user to a document."""

    def post(self, request, *args, **kwargs) -> HttpResponse:
        document = get_object_or_404(Document, pk=kwargs["pk"])
        DocumentSubscription.objects.get_or_create(user=request.user, document=document)
        return redirect("monitor:document_detail", pk=document.pk)


class DocumentUnsubscribeView(LoginRequiredMixin, View):
    """POST to unsubscribe the signed-in user from a document."""

    def post(self, request, *args, **kwargs) -> HttpResponse:
        DocumentSubscription.objects.filter(user=request.user, document_id=kwargs["pk"]).delete()
        return redirect("monitor:document_detail", pk=kwargs["pk"])


class OrganizationSubscribeView(LoginRequiredMixin, View):
    """POST to subscribe the signed-in user to all docs for an organization."""

    def post(self, request, *args, **kwargs) -> HttpResponse:
        organization = get_object_or_404(Organization, pk=kwargs["pk"])
        OrganizationSubscription.objects.get_or_create(user=request.user, organization=organization)
        return redirect("monitor:organizations")


class OrganizationUnsubscribeView(LoginRequiredMixin, View):
    """POST to unsubscribe the signed-in user from an organization."""

    def post(self, request, *args, **kwargs) -> HttpResponse:
        OrganizationSubscription.objects.filter(
            user=request.user, organization_id=kwargs["pk"]
        ).delete()
        return redirect("monitor:organizations")


class UnsubscribeTokenView(TemplateView):
    """
    One-click unsubscribe for change notifications.

    The signed *token* encodes the (user, document) pair; visiting the link
    removes the matching document subscription so email clients can unsubscribe
    without logging in.  Organization-level subscriptions are left untouched.
    """

    template_name = "monitor/unsubscribe_confirmed.html"

    def get(self, request, *args, **kwargs) -> HttpResponse:
        try:
            payload = read_unsubscribe_token(kwargs["token"])
        except (signing.BadSignature, TypeError, ValueError):
            raise Http404("Invalid or expired unsubscribe link.") from None
        DocumentSubscription.objects.filter(
            user_id=payload["user_id"], document_id=payload["document_id"]
        ).delete()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "You're unsubscribed"
        return context


# ---------------------------------------------------------------------------
# Superuser management views
# ---------------------------------------------------------------------------


class SuperuserRequiredMixin(UserPassesTestMixin):
    """Restrict a view to logged-in superusers only."""

    login_url = "/accounts/login/"

    def test_func(self) -> bool:
        user = self.request.user
        return bool(user.is_authenticated and user.is_superuser)


class ManageDashboardView(SuperuserRequiredMixin, TemplateView):
    """Landing page for the superuser management area."""

    template_name = "monitor/manage/dashboard.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Manage TosDiff"
        context["organization_count"] = Organization.objects.count()
        context["document_count"] = Document.objects.count()
        context["pending_suggestion_count"] = Suggestion.objects.filter(
            status=Suggestion.Status.PENDING
        ).count()
        context["approved_document_count"] = _tracked_organizations().count()
        context["country_count"] = Country.objects.count()
        context["language_count"] = Language.objects.count()
        return context


class ManageOrganizationListView(SuperuserRequiredMixin, ListView):
    """List all organizations with their document counts."""

    model = Organization
    template_name = "monitor/manage/organization_list.html"
    context_object_name = "organizations"

    def get_queryset(self):
        return (
            Organization.objects.annotate(document_count=Count("documents", distinct=True))
            .select_related("parent")
            .order_by("name")
        )

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Manage organizations"
        return context


class ManageOrganizationCreateView(SuperuserRequiredMixin, CreateView):
    model = Organization
    form_class = OrganizationForm
    template_name = "monitor/manage/organization_form.html"
    success_url = reverse_lazy("monitor:manage_organizations")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Add organization"
        return context


class ManageOrganizationUpdateView(SuperuserRequiredMixin, UpdateView):
    model = Organization
    form_class = OrganizationForm
    template_name = "monitor/manage/organization_form.html"
    success_url = reverse_lazy("monitor:manage_organizations")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit {self.object.name}"
        return context


class ManageDocumentListView(SuperuserRequiredMixin, ListView):
    """List all documents, optionally filtered by organization and type."""

    model = Document
    template_name = "monitor/manage/document_list.html"
    context_object_name = "documents"
    paginate_by = 50

    def get_queryset(self):
        qs = Document.objects.select_related("organization", "language", "country").order_by(
            "organization__name", "document_type", "url"
        )
        organization_id = self.request.GET.get("organization")
        document_type = self.request.GET.get("type")
        if organization_id:
            qs = qs.filter(organization_id=organization_id)
        if document_type:
            qs = qs.filter(document_type=document_type)
        return qs

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Manage documents"
        context["organizations"] = Organization.objects.order_by("name")
        context["document_types"] = Document.DocumentType.choices
        context["selected_organization"] = self.request.GET.get("organization", "")
        context["selected_doc_type"] = self.request.GET.get("type", "")
        return context


class ManageDocumentCreateView(SuperuserRequiredMixin, CreateView):
    model = Document
    form_class = DocumentForm
    template_name = "monitor/manage/document_form.html"
    success_url = reverse_lazy("monitor:manage_documents")

    def get_initial(self) -> dict:
        initial = super().get_initial()
        organization_pk = self.kwargs.get("organization_pk")
        if organization_pk:
            initial["organization"] = organization_pk
        return initial

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Add document"
        return context


class ManageDocumentUpdateView(SuperuserRequiredMixin, UpdateView):
    model = Document
    form_class = DocumentForm
    template_name = "monitor/manage/document_form.html"
    success_url = reverse_lazy("monitor:manage_documents")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit {self.object.display_name}"
        return context


class ManageSuggestionListView(SuperuserRequiredMixin, ListView):
    """List user-submitted suggestions, defaulting to pending ones."""

    model = Suggestion
    template_name = "monitor/manage/suggestion_list.html"
    context_object_name = "suggestions"
    paginate_by = 50

    def get_queryset(self):
        qs = Suggestion.objects.annotate(
            is_duplicate=Exists(Organization.objects.filter(website_url=OuterRef("website_url")))
        ).order_by("-submitted_at", "-pk")
        status = self.request.GET.get("status", Suggestion.Status.PENDING)
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Manage suggestions"
        context["statuses"] = Suggestion.Status.choices
        context["selected_status"] = self.request.GET.get("status", Suggestion.Status.PENDING)
        return context


class ManageSuggestionReviewView(SuperuserRequiredMixin, FormView):
    """Approve (convert) or reject a submitted suggestion, per URL action."""

    template_name = "monitor/manage/suggestion_review.html"
    form_class = SuggestionReviewForm
    action = ""  # "approve" or "reject", set per URL pattern

    @property
    def success_url(self):
        return reverse_lazy("monitor:manage_suggestions")

    def dispatch(self, request, *args, **kwargs):
        self.suggestion = get_object_or_404(Suggestion, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["suggestion"] = self.suggestion
        context["action"] = self.action
        context["is_duplicate"] = Organization.objects.filter(
            website_url=self.suggestion.website_url
        ).exists()
        context["page_title"] = (
            f"Approve suggestion: {self.suggestion.organization_name}"
            if self.action == "approve"
            else f"Reject suggestion: {self.suggestion.organization_name}"
        )
        return context

    def form_valid(self, form) -> HttpResponse:
        if self.suggestion.status != Suggestion.Status.PENDING:
            return super().form_valid(form)
        created_document = None
        if self.action == "approve":
            _, created_document = self.suggestion.create_organization_and_document()
            self.suggestion.status = Suggestion.Status.APPROVED
        elif self.action == "reject":
            self.suggestion.status = Suggestion.Status.REJECTED
        self.suggestion.review_notes = form.cleaned_data["review_notes"]
        self.suggestion.reviewed_at = timezone.now()
        self.suggestion.save()
        send_suggestion_review_email(
            self.suggestion,
            approved=self.action == "approve",
            site_url=settings.BASE_URL,
            review_notes=self.suggestion.review_notes,
            document=created_document,
        )
        return super().form_valid(form)


class ManageTagListView(SuperuserRequiredMixin, ListView):
    model = Tag
    template_name = "monitor/manage/tag_list.html"
    context_object_name = "tags"
    paginate_by = 50

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Manage tags"
        return context


class ManageTagCreateView(SuperuserRequiredMixin, CreateView):
    model = Tag
    form_class = TagForm
    template_name = "monitor/manage/tag_form.html"
    success_url = reverse_lazy("monitor:manage_tags")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Add tag"
        return context


class ManageTagUpdateView(SuperuserRequiredMixin, UpdateView):
    model = Tag
    form_class = TagForm
    template_name = "monitor/manage/tag_form.html"
    success_url = reverse_lazy("monitor:manage_tags")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Edit tag"
        return context


class ManageCountryListView(SuperuserRequiredMixin, ListView):
    """List all countries available for documents."""

    model = Country
    template_name = "monitor/manage/country_list.html"
    context_object_name = "countries"
    paginate_by = 50

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Manage countries"
        return context


class ManageCountryCreateView(SuperuserRequiredMixin, CreateView):
    model = Country
    form_class = CountryForm
    template_name = "monitor/manage/country_form.html"
    success_url = reverse_lazy("monitor:manage_countries")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Add country"
        return context


class ManageCountryUpdateView(SuperuserRequiredMixin, UpdateView):
    model = Country
    form_class = CountryForm
    template_name = "monitor/manage/country_form.html"
    success_url = reverse_lazy("monitor:manage_countries")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit country: {self.object.name}"
        return context


class ManageLanguageListView(SuperuserRequiredMixin, ListView):
    """List all languages available for documents."""

    model = Language
    template_name = "monitor/manage/language_list.html"
    context_object_name = "languages"
    paginate_by = 50

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Manage languages"
        return context


class ManageLanguageCreateView(SuperuserRequiredMixin, CreateView):
    model = Language
    form_class = LanguageForm
    template_name = "monitor/manage/language_form.html"
    success_url = reverse_lazy("monitor:manage_languages")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Add language"
        return context


class ManageLanguageUpdateView(SuperuserRequiredMixin, UpdateView):
    model = Language
    form_class = LanguageForm
    template_name = "monitor/manage/language_form.html"
    success_url = reverse_lazy("monitor:manage_languages")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit language: {self.object.name}"
        return context


class ManageAttentionView(SuperuserRequiredMixin, TemplateView):
    """Surfaces documents that need attention (failing, never checked, no snapshots)."""

    template_name = "monitor/manage/attention.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Needs attention"
        context["failing_organizations"] = list(
            Organization.objects.filter(is_failing=True).order_by("name")
        )
        context["failing_documents"] = list(
            Document.objects.filter(is_failing=True)
            .select_related("organization")
            .order_by("organization__name", "document_type")
        )
        context["never_checked_documents"] = list(
            Document.objects.filter(last_checked__isnull=True)
            .select_related("organization")
            .order_by("organization__name", "document_type")
        )
        context["no_snapshot_documents"] = list(
            Document.objects.filter(snapshots__isnull=True)
            .select_related("organization")
            .order_by("organization__name", "document_type")
        )
        return context


class ManageUserListView(SuperuserRequiredMixin, ListView):
    """Browse registered users and their subscription counts."""

    model = get_user_model()
    template_name = "monitor/manage/user_list.html"
    context_object_name = "users"
    paginate_by = 50

    def get_queryset(self):
        qs = (
            get_user_model()
            .objects.annotate(
                document_subscription_count=Count("document_subscriptions", distinct=True),
                organization_subscription_count=Count("organization_subscriptions", distinct=True),
            )
            .order_by("-is_superuser", "email")
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(Q(email__icontains=query) | Q(username__icontains=query))
        return qs

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Users"
        context["query"] = self.request.GET.get("q", "").strip()
        return context


class ManageDocumentCheckView(SuperuserRequiredMixin, View):
    """POST-only: enqueue a fetch-and-snapshot for the given document now."""

    def post(self, request, *args, **kwargs) -> HttpResponse:
        document = get_object_or_404(Document, pk=kwargs["pk"])
        check_document.delay(document.pk)
        messages.success(request, f"Fetch queued for {document.display_name}.")
        return redirect("monitor:manage_documents")


class ManageOrganizationDeleteView(SuperuserRequiredMixin, DeleteView):
    model = Organization
    template_name = "monitor/manage/confirm_delete.html"
    success_url = reverse_lazy("monitor:manage_organizations")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Delete {self.object.name}"
        context["cancel_url"] = reverse_lazy("monitor:manage_organizations")
        context["warnings"] = [
            f"{self.object.documents.count()} tracked document(s) and their snapshots will be deleted.",
            "All user subscriptions to this organization and its documents will be removed.",
        ]
        return context


class ManageDocumentDeleteView(SuperuserRequiredMixin, DeleteView):
    model = Document
    template_name = "monitor/manage/confirm_delete.html"
    success_url = reverse_lazy("monitor:manage_documents")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Delete {self.object.display_name}"
        context["cancel_url"] = reverse_lazy("monitor:manage_documents")
        context["warnings"] = [
            f"{self.object.snapshots.count()} historical snapshot(s) will be deleted.",
            "User subscriptions to this document will be removed.",
        ]
        return context


class ManageTagDeleteView(SuperuserRequiredMixin, DeleteView):
    model = Tag
    template_name = "monitor/manage/confirm_delete.html"
    success_url = reverse_lazy("monitor:manage_tags")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Delete tag: {self.object.name}"
        context["cancel_url"] = reverse_lazy("monitor:manage_tags")
        context["warnings"] = [
            "The tag will be removed from all organizations it is attached to.",
        ]
        return context


class ManageCountryDeleteView(SuperuserRequiredMixin, DeleteView):
    model = Country
    template_name = "monitor/manage/confirm_delete.html"
    success_url = reverse_lazy("monitor:manage_countries")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Delete country: {self.object.name}"
        context["cancel_url"] = reverse_lazy("monitor:manage_countries")
        document_count = Document.objects.filter(country=self.object).count()
        context["warnings"] = [
            f"{document_count} document(s) currently reference this country. "
            "Their country field will be cleared.",
        ]
        return context


class ManageLanguageDeleteView(SuperuserRequiredMixin, DeleteView):
    model = Language
    template_name = "monitor/manage/confirm_delete.html"
    success_url = reverse_lazy("monitor:manage_languages")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Delete language: {self.object.name}"
        context["cancel_url"] = reverse_lazy("monitor:manage_languages")
        document_count = Document.objects.filter(language=self.object).count()
        if document_count:
            context["warnings"] = [
                f"{document_count} document(s) reference this language. "
                "Deletion is blocked until those documents are reassigned.",
            ]
        return context

    def form_valid(self, form) -> HttpResponse:
        success_url = self.get_success_url()
        try:
            self.object.delete()
        except ProtectedError:
            messages.error(
                self.request,
                f'"{self.object}" is referenced by documents and cannot be deleted.',
            )
        else:
            messages.success(self.request, f'Deleted language "{self.object}".')
        return redirect(success_url)


class ManageSuggestionDeleteView(SuperuserRequiredMixin, DeleteView):
    model = Suggestion
    template_name = "monitor/manage/confirm_delete.html"
    success_url = reverse_lazy("monitor:manage_suggestions")

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Delete suggestion: {self.object.organization_name}"
        context["cancel_url"] = reverse_lazy("monitor:manage_suggestions")
        context["warnings"] = ["This removes the submitted suggestion record permanently."]
        return context
