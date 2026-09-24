"""Forms for user-submitted website suggestions, passwordless (OTP) login, and superuser management."""

import json

from django import forms

from .models import Document, NotificationPreference, Organization, Suggestion, Tag


class SuggestionForm(forms.ModelForm):
    """Public form allowing anyone to suggest a company and a policy URL."""

    class Meta:
        model = Suggestion
        fields = [
            "organization_name",
            "website_url",
            "document_url",
            "document_type",
            "other_document_type",
            "contact_email",
            "notes",
        ]
        widgets = {
            "organization_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. Acme Corp",
                }
            ),
            "website_url": forms.URLInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://example.com",
                }
            ),
            "document_url": forms.URLInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://example.com/terms",
                }
            ),
            "document_type": forms.Select(attrs={"class": "form-select"}),
            "other_document_type": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. End-User License Agreement",
                }
            ),
            "contact_email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "you@example.com",
                }
            ),
            "notes": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Anything we should know about this document?",
                }
            ),
        }


class EmailLoginForm(forms.Form):
    """Collects the email address to send a one-time login code to."""

    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "autocomplete": "email",
                "autofocus": True,
                "placeholder": "you@example.com",
            }
        ),
    )

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().lower()


class CodeLoginForm(forms.Form):
    """Collects the six-digit code emailed to the user."""

    code = forms.CharField(
        label="Login code",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "autofocus": True,
                "placeholder": "6-digit code",
            }
        ),
    )

    def clean_code(self) -> str:
        code = self.cleaned_data["code"].strip()
        if not code.isdigit():
            raise forms.ValidationError("Enter the 6-digit code from your email.")
        return code


# ---------------------------------------------------------------------------
# Superuser management forms
# ---------------------------------------------------------------------------


class _JsonTextarea(forms.Textarea):
    """Textarea that pretty-prints JSON for editing and shows blank for None."""

    def format_value(self, value):
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return super().format_value(value)


class TagForm(forms.ModelForm):
    """Superuser form for creating/editing tags."""

    class Meta:
        model = Tag
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autofocus": True,
                    "placeholder": "e.g. Governance",
                }
            ),
        }


class OrganizationForm(forms.ModelForm):
    """Superuser form for creating/editing tracked organizations."""

    class Meta:
        model = Organization
        fields = ["name", "website_url", "category", "tags", "parent", "is_failing"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "autofocus": True}),
            "website_url": forms.URLInput(attrs={"class": "form-control"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "tags": forms.SelectMultiple(attrs={"class": "form-select"}),
            "parent": forms.Select(attrs={"class": "form-select"}),
            "is_failing": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class DocumentForm(forms.ModelForm):
    """Superuser form for creating/editing tracked documents."""

    class Meta:
        model = Document
        fields = [
            "organization",
            "name",
            "document_type",
            "other_document_type",
            "url",
            "fetch_method",
            "document_format",
            "language",
            "country",
            "is_active",
            "is_failing",
            "custom_selectors",
            "fetch_config",
        ]
        widgets = {
            "organization": forms.Select(attrs={"class": "form-select"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "document_type": forms.Select(attrs={"class": "form-select"}),
            "other_document_type": forms.TextInput(attrs={"class": "form-control"}),
            "url": forms.URLInput(attrs={"class": "form-control"}),
            "fetch_method": forms.Select(attrs={"class": "form-select"}),
            "document_format": forms.Select(attrs={"class": "form-select"}),
            "language": forms.Select(attrs={"class": "form-select"}),
            "country": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_failing": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "custom_selectors": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "fetch_config": _JsonTextarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": '{"wait_for_selector": ".content"}',
                }
            ),
        }


class NotificationPreferenceForm(forms.ModelForm):
    """User-facing form to choose a daily or weekly change-email digest."""

    class Meta:
        model = NotificationPreference
        fields = ["frequency"]
        widgets = {
            "frequency": forms.Select(attrs={"class": "form-select"}),
        }


class SuggestionReviewForm(forms.Form):
    """Superuser form used when approving or rejecting a submitted suggestion."""

    review_notes = forms.CharField(
        required=False,
        label="Review notes (optional)",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
