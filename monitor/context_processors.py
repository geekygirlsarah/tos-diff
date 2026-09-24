"""Template context processors for the monitor app."""

from django.conf import settings


def sponsor_links(request):
    """Expose configured funding links so the footer can render support buttons.

    Returns empty strings when a URL is unset, and the footer template uses
    them to conditionally show each button (so nothing renders unless the
    maintainer has configured at least one funding link).
    """
    return {
        "sponsor_github_url": settings.SPONSOR_GITHUB_URL,
        "sponsor_kofi_url": settings.SPONSOR_KOFI_URL,
    }
