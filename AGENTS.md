# TosDiff — AI Agent Development Guidelines

## Project Overview

**TosDiff** is a Django application that monitors changes to legal documents (Terms of Service, Privacy Policies, etc.) across multiple websites. It fetches document content on a schedule, stores snapshots, detects changes via SHA-256 hashing, and presents diffs through a web UI.

---

## Agent Conduct

### Core Principles

- **Always follow Test-Driven Development (TDD):** When implementing a new feature or fixing a bug, you MUST first provide the test case that reproduces the issue or defines the new behavior. Only then provide the implementation.
- **Respect read-only mode:** Do not modify files unless explicitly asked.
- **Follow existing style:** Match the project's coding conventions (see Code Style below).
- **No interactive commands:** All terminal commands must be non-interactive. Never use flags that prompt for input.
- **Don't make large assumptions:** If something is unclear, ask before making assumptions about requirements, data flow, or business logic.

### General Conduct

- Run the full test suite after every meaningful change: `python manage.py test monitor --verbosity=2`
- Run the linter after every change: `ruff check . && ruff format --check .`
- Never commit secrets, API keys, or credentials. Use environment variables.
- Never modify existing migrations without careful consideration. Add new ones.
- Keep `services.py` functions pure; no Django ORM calls except in `create_snapshot_if_changed` and `fetch_and_snapshot`.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | Django 6.0+ (class-based views, ORM, admin) |
| Language | Python 3.12+ |
| Database | PostgreSQL (production) / SQLite (local dev) |
| Task queue | Celery 5.3+ with Redis broker |
| HTML fetching | `requests` library (default) or Playwright (JS-heavy sites) |
| HTML parsing | BeautifulSoup4 + lxml |
| PDF extraction | pdfplumber |
| Web server | Gunicorn (production) |
| Frontend | Bootstrap 5 (responsive, accessible) |
| Containerisation | Docker + docker-compose |
| CI/CD | GitHub Actions |
| Linting | Ruff |
| Testing | Django `TestCase` + `unittest.mock` |

---

## Repository Layout

```
TosDiff-New/
├── tosdiff_new/          # Django project package
│   ├── settings.py       # All config; secrets via env vars
│   ├── celery.py         # Celery app instance
│   ├── urls.py           # Root URL conf
│   ├── wsgi.py / asgi.py
│   └── __init__.py       # Imports celery_app so it loads with Django
├── monitor/              # Main application
│   ├── models.py         # All data models
│   ├── admin.py          # Django admin configuration
│   ├── views.py          # Class-based views
│   ├── urls.py           # App URL patterns (app_name = "monitor")
│   ├── services.py       # Pure fetch/parse/snapshot logic (Celery-ready)
│   ├── tasks.py          # Celery tasks
│   ├── tests.py          # All unit tests
│   └── management/commands/fetch_documents.py
├── templates/
│   ├── base.html         # Bootstrap 5 base layout
│   └── monitor/          # App-specific templates
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── pyproject.toml        # Dependencies + coverage config
├── ruff.toml             # Linting rules
└── .github/workflows/ci.yml
```

---

## Code Style

- **Linting:** Use `ruff check .` and `ruff format --check .` (rules defined in `ruff.toml`).
- **Formatting:** Ruff handles formatting with `quote-style = "double"` and `indent-style = "space"`, line length 100.
- **PEP 8:** Follow standard Python style. Ruff enforces pycodestyle (E/W), pyflakes (F), isort (I), bugbear (B), and pyupgrade (UP).
- **Type hints:** Required on all function signatures in `services.py` and `views.py`.
- **Admin:** Use meaningful `verbose_name` and `help_text` on model fields. Keep `list_display` performant with `select_related`/`prefetch_related`. Use `format_html()` for any HTML in admin methods — never string concatenation.
- **Templates:** Use `{% url 'monitor:name' %}` (namespaced). Use Bootstrap 5 classes. Ensure accessible markup (ARIA labels, semantic HTML).
- **Service functions:** Keep pure and side-effect-free. Include type hints. External calls (HTTP, filesystem) must be mockable.
- **Migrations:** Maintain validators and preserve unique constraints. Do not remove or modify existing migrations without careful consideration.

---

## Testing

### Running Tests

```bash
python manage.py test monitor --verbosity=2
```

### Test Conventions

- Use `django.test.TestCase` for all tests.
- Mock all external HTTP calls with `@patch("monitor.services.requests.get")` or `@patch("monitor.services.fetch_pdf_bytes")`.
- Use `get_or_create` when referencing seeded data (e.g. the English language seeded by migration 0003).
- Group tests by class: one `TestCase` subclass per model/service/view area.
- Aim for ≥ 80% coverage (enforced in CI).

### Test-Driven Development

When implementing a new feature or fixing a bug:

1. Write a failing test that captures the expected behavior or reproduces the bug.
2. Run the test to confirm it fails.
3. Write the minimum implementation to make the test pass.
4. Run the full test suite to confirm nothing is broken.
5. Refactor if needed, keeping tests green.

---

## Development Workflows

### Adding a new model field
1. Add the field to `monitor/models.py`
2. Run `python manage.py makemigrations monitor --name="descriptive_name"`
3. If the field has a default or is nullable, the migration is straightforward; otherwise provide a default
4. For data migrations (seeding), use `RunPython` in the migration file
5. Update `monitor/admin.py` — add to `list_display`, `list_filter`, and relevant `fieldsets`
6. Add tests in `monitor/tests.py`

### Adding a new `DocumentType`
1. Add the choice to `Document.DocumentType` in `models.py` (before `OTHER`)
2. Run `python manage.py makemigrations monitor --name="<type_name>"`
3. Add the new type to `test_new_document_types_exist` in `tests.py`

### Adding a new view
1. Create a CBV in `monitor/views.py`
2. Add the URL pattern to `monitor/urls.py` with a `name`
3. Create the template in `templates/monitor/`
4. Extend `base.html`; use Bootstrap 5 classes; ensure accessible markup (ARIA labels, semantic HTML)
5. Add view tests in `monitor/tests.py`

### Adding a new service function
1. Add to `monitor/services.py` as a pure function with type hints
2. Keep it side-effect-free and Celery-friendly
3. Add unit tests with `unittest.mock.patch` for external calls (HTTP, filesystem)

---

## Superuser Management Pages

Superusers get a custom management area (in addition to Django admin) under `/manage/`:

- `manage_dashboard` — counts + quick-action links to every management page
- `manage_organizations`, `manage_organization_create`, `manage_organization_update` — CRUD for `Organization`; `manage_organization_delete` confirms + cascades
- `manage_documents`, `manage_document_create`, `manage_document_update` — CRUD for `Document`; `manage_document_create_for_organization` (`/manage/organizations/<pk>/documents/add/`) preselects the organization; `manage_document_delete`; `manage_document_check` (POST) triggers `check_document.delay(document.pk)` immediately
- `manage_suggestions`, `manage_suggestion_approve`, `manage_suggestion_reject` — review queue; approving calls `Suggestion.create_organization_and_document()` and marks the suggestion approved; `manage_suggestion_delete`. The list view annotates `is_duplicate` (an `Organization` already exists with the same `website_url`) and the review page warns accordingly. Approving/rejecting emails the submitter via `send_suggestion_review_email()` (skipped when no `contact_email`).
- `manage_tags`, `manage_tag_create`, `manage_tag_update`, `manage_tag_delete` — CRUD for `Tag`
- `manage_attention` (`/manage/attention/`) — needs-attention panel (failing/inactive/never-checked documents)
- `manage_users` (`/manage/users/`) — user overview with search, pagination, and `document_subscription_count` / `organization_subscription_count` annotations

Conventions:
- Every view subclasses `SuperuserRequiredMixin` in `monitor/views.py` (redirects anonymous users to login, returns 403 for non-superusers).
- Forms live in `monitor/forms.py` (`OrganizationForm`, `DocumentForm`, `TagForm`, `SuggestionReviewForm`); they style fields with `form-control`/`form-select` and use the shared `_field.html` partial in templates.
- Templates live in `templates/monitor/manage/`; they are responsive (`.table-responsive` wrappers, `flex-wrap`, `col-*` grids). Edit forms show a "Delete" link (and document forms a "Check now" button) when editing an existing object.
- The navbar shows an "Admin" link only to superusers.
- Keep URL names `manage_*` and prefix all routes with `/manage/`.
- Delete flows share the `confirm_delete.html` template and the message-flash block in `base.html`.

## Email Delivery & Subscription Preferences

- Change notification emails (`build_snapshot_change_message` in `services.py`) include an absolute one-click unsubscribe link signed with `make_unsubscribe_token()` (`services.py`). `UnsubscribeTokenView` (`/unsubscribe/<token>/`) removes the matching `DocumentSubscription` without login (invalid/expired tokens → 404); organization subscriptions are untouched.
- `NotificationPreference` (OneToOne with user, `frequency` in immediate/daily/weekly) controls delivery. `send_change_notifications` emails immediately by default, otherwise queues a `PendingNotification`. `send_daily_digests` / `send_weekly_digests` (Celery tasks, wired into `CELERY_BEAT_SCHEDULE` in `settings.py`) send one digest email per user with links + unsubscribe per document, then clear that user's queue. The account page (`monitor/account.html`) edits the preference and lists `my_suggestions`.
- `Suggestion.user` (nullable FK, `SET_NULL`) records the logged-in submitter; anonymous submissions leave it null.

## UI & Templates

- The site must be **responsive and mobile-first**: every page should work and look good from small phone screens up to wide desktop viewports. Use Bootstrap's responsive utilities (`row`/`col-*` grid, `flex-wrap`, `table-responsive`, `gap-*`) so content wraps and reflows instead of overflowing. All new pages must verify their layout at a small viewport width before being considered done.
- The homepage (`monitor/home.html`) supports two query params: `days` (3, 7, 14, 30) and `type` (a valid `Document.DocumentType` value). Invalid values fall back to defaults. The distinct types present in the current time-window are passed as `document_types` in the view context. Snapshots are grouped into `day_groups` (day → organization → snapshots), ordered by day (newest first), then organization name (A–Z); `RecentChangesView` in `monitor/views.py` builds this structure and shows dates only (no timestamps). Its hero shows `total_organizations` / `total_documents` stats in a two-column layout.
- The organizations page (`monitor/organizations.html`) shows a hero banner with `total_organizations` / `total_documents` stats plus per-organization cards; `OrganizationsView` provides those counts in context. Both pages share the `_tracked_organizations()` helper in `monitor/views.py` for the org count.
- Organization favicons render next to org names using `Organization.favicon_url` (DuckDuckGo's icon service) with lazy loading and a hidden fallback on error.
- Shared UI styling lives in `templates/base.html` as CSS custom properties under the `--td-*` variables (blues/greens palette). Custom classes are prefixed `td-` (e.g. `td-card`, `td-chip`, `td-badge`, `td-card-row`).

---

## CI/CD (GitHub Actions)

Three jobs run on every push/PR to `main`/`master`:

1. **test** — Python 3.14; runs migrations, tests, and coverage (≥ 80% required)
2. **security** — `pip-audit --strict` for known dependency vulnerabilities
3. **lint** — `ruff check` + `ruff format --check`

All three jobs must pass before merging.

---

## Data Models

### `Country`
- `name` — display name (e.g. "United States")
- `code` — ISO 3166-1 alpha-2, unique (e.g. "US")

### `Language`
- `name` — display name (e.g. "English", "Français")
- `code` — ISO 639-1, unique (e.g. "en", "fr")

### `Tag`
- `name` — unique display name
- `slug` — auto-generated from name if blank

### `Organization`
- `name`, `slug` (auto-generated from name if blank), `website_url`
- `category` — `TextChoices` enum (technology, financial, healthcare, entertainment_streaming, media, social_media, hospitality, retail, other)
- `tags` — M2M to `Tag`
- `parent` — self-referential FK (`SET_NULL`); subsidiaries accessible via `org.subsidiaries.all()`
- `is_failing` — flag for orgs with consistently failing documents

### `Document`
- FK to `Organization`
- `name` — optional custom name; falls back to document type display name
- `document_type` — `TextChoices` enum (tos, privacy, cookie, refund, childrens_privacy, subscription, service_agreement, service_fees, user_agreement, conduct, acceptable_use, dmca, payment_service, other)
- `other_document_type` — free-text label when `document_type = "other"`
- `url`, `fetch_method` (`requests` or `playwright`), `document_format` (`html`, `pdf`, `txt`)
- `language` FK → `Language` (nullable, `PROTECT`)
- `country` FK → `Country` (nullable, `SET_NULL`)
- `last_checked`, `last_changed` (DateTimeField, nullable)
- `is_active` (BooleanField, default True)
- `is_failing` (BooleanField, default False)
- `custom_selectors` — CSS selectors to strip before extraction, one per line
- `fetch_config` — JSONField for Playwright options (`wait_for_selector`, `sleep_seconds`, `dismiss_selectors`)
- Unique together: `(organization, document_type, url)`

### `DocumentSnapshot`
- FK to `Document` (CASCADE)
- `captured_at` (auto_now_add)
- `cleaned_text` — markdown-like plain text
- `text_hash` — SHA-256 of `cleaned_text` (indexed); used for deduplication

### `Suggestion`
- `organization_name`, `website_url`, `document_url`, `document_type`, `other_document_type`
- `contact_email` (optional; checked before any review email is sent), `notes`
- `user` — nullable FK to the logged-in submitter (`SET_NULL`); null for anonymous submissions
- `status` (pending/approved/rejected), `submitted_at`, `reviewed_at`, `review_notes`
- `create_organization_and_document()` — idempotent get_or_create of an `Organization` + `Document`

### `NotificationPreference`
- OneToOne with user; `frequency` in `TextChoices` (immediate/daily/weekly), defaults to immediate
- Absence of a row means immediate delivery (so task behavior stays compatible)

### `PendingNotification`
- Queued change notification for digest users: `user`, `document`, `snapshot`, `old_snapshot` (nullable), `created_at`
- Rows are created by `send_change_notifications` for daily/weekly users and deleted by `send_daily_digests` / `send_weekly_digests` after the digest email is sent

---

## Environment Variables

All secrets and connection strings are read from environment variables. See `.env.example` for the full list. **Never commit `.env` files.**

---

## Documentation

Update `AGENTS.md` with any significant changes to the project architecture, new models, new views, or changes to the development workflow.
