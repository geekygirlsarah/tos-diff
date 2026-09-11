"""Forms for user-submitted website suggestions and passwordless (OTP) login."""

from django import forms

from .models import Suggestion


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
