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

## UI & Templates

- The site must be **responsive and mobile-first**: every page should work and look good from small phone screens up to wide desktop viewports. Use Bootstrap's responsive utilities (`row`/`col-*` grid, `flex-wrap`, `table-responsive`, `gap-*`) so content wraps and reflows instead of overflowing. All new pages must verify their layout at a small viewport width before being considered done.
- The homepage (`monitor/home.html`) supports two query params: `days` (3, 7, 14, 30) and `type` (a valid `Document.DocumentType` value). Invalid values fall back to defaults. The distinct types present in the current time-window are passed as `document_types` in the view context (`monitor/views.py:RecentChangesView`).
- Shared UI styling lives in `templates/base.html` as CSS custom properties under the `--td-*` variables (blues/greens palette). Custom classes are prefixed `td-` (e.g. `td-card`, `td-chip`, `td-badge`).

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

---

## Environment Variables

All secrets and connection strings are read from environment variables. See `.env.example` for the full list. **Never commit `.env` files.**

---

## Documentation

Update `AGENTS.md` with any significant changes to the project architecture, new models, new views, or changes to the development workflow.
