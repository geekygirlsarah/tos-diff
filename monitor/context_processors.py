"""Template context processors for the monitor app."""

import subprocess
from datetime import date

from django.conf import settings

_UNSET = object()
_last_commit_date_cache = _UNSET


def _get_last_commit_date() -> date | None:
    """Return the date of the repository's latest commit, or None when unavailable.

    Runs ``git log`` against the project checkout and caches the result for the
    lifetime of the process so page requests do not spawn a subprocess each time.
    Returns None when git is missing, the checkout is not a repository, or the
    command fails for any reason; the footer hides the "Last updated" line then.
    """
    global _last_commit_date_cache
    if _last_commit_date_cache is not _UNSET:
        return _last_commit_date_cache
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "-C",
                str(settings.BASE_DIR),
                "log",
                "-1",
                "--format=%cs",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        output = result.stdout.strip()
        _last_commit_date_cache = date.fromisoformat(output) if output else None
    except Exception:
        _last_commit_date_cache = None
    return _last_commit_date_cache


def last_updated(request):
    """Expose the repository's last commit date for the footer's "Last updated" line.

    The template formats it as a month/year ("May 2026") and omits the line
    entirely when the date could not be determined.
    """
    return {"last_updated": _get_last_commit_date()}


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
