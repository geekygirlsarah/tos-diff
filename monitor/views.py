import difflib
import secrets
from itertools import groupby
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model, login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LogoutView  # noqa: F401 – re-exported for urls
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView
from django.views.generic.edit import FormView

from .forms import CodeLoginForm, EmailLoginForm, SuggestionForm
from .models import (
    Document,
    DocumentSnapshot,
    DocumentSubscription,
    LoginCode,
    Organization,
    OrganizationSubscription,
)
from .services import (
    LOGIN_CODE_TTL_MINUTES,
    compute_hash,
    generate_login_code,
    send_login_code_email,
)

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
        form.save()
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
    """Signed-in user's dashboard: list their subscriptions."""

    template_name = "monitor/account.html"
    login_url = "/accounts/login/"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Your account"
        context["document_subscriptions"] = (
            DocumentSubscription.objects.filter(user=self.request.user)
            .select_related("document__organization")
            .order_by("document__organization__name", "document__document_type")
        )
        context["organization_subscriptions"] = (
            OrganizationSubscription.objects.filter(user=self.request.user)
            .select_related("organization")
            .order_by("organization__name")
        )
        return context


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
