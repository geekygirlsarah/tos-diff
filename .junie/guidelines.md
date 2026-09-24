# TosDiff — Junie Development Guidelines

## Project Overview

**TosDiff** is a Django application that monitors changes to legal documents (Terms of Service, Privacy Policies, etc.) across multiple websites. It fetches document content on a schedule, stores snapshots, detects changes via SHA-256 hashing, and presents diffs through a web UI.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | Django 6.0+ (class-based views, ORM, admin) |
| Language | Python 3.12+ |
| Database | PostgreSQL (production) / SQLite (local dev) |
| Task scheduling | Daily cron job (`python manage.py fetch_documents`); no Celery/Redis |
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
├── tosdiff/          # Django project package
│   ├── settings.py       # All config; secrets via env vars
│   ├── urls.py           # Root URL conf
│   ├── wsgi.py / asgi.py
│   └── __init__.py
├── monitor/              # Main application
│   ├── models.py         # All data models
│   ├── admin.py          # Django admin configuration
│   ├── views.py          # Class-based views
│   ├── urls.py           # App URL patterns (app_name = "monitor")
│   ├── services.py       # Pure fetch/parse/snapshot logic (command & view friendly)
│   ├── tasks.py          # Document checks, change-notification queueing, digests (synchronous)
│   ├── tests.py          # All unit tests
│   └── management/commands/fetch_documents.py   # Daily cycle: fetch → queue → digests
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

## Data Models

### `Country`
- `name` — display name (e.g. "United States")
- `code` — ISO 3166-1 alpha-2, unique (e.g. "US")
- Ordered by `name`

### `Language`
- `name` — display name (e.g. "English", "Français")
- `code` — ISO 639-1, unique (e.g. "en", "fr")
- Ordered by `name`

### `Organization`
- `name`, `slug` (auto-generated from name if blank), `website_url`
- `parent` — self-referential FK (`SET_NULL`); subsidiaries accessible via `org.subsidiaries.all()`
- Example: Meta is parent of Facebook and Instagram

### `Document`
- FK to `Organization`
- `document_type` — `TextChoices` enum (see full list below)
- `other_document_type` — free-text label when `document_type = "other"`
- `url`, `fetch_method` (`requests` or `playwright`), `document_format` (`html`, `pdf`, `txt`)
- `language` FK → `Language` (nullable, `PROTECT`)
- `country` FK → `Country` (nullable, `SET_NULL`)
- `last_checked`, `last_changed` (DateTimeField, nullable)
- `is_active` (BooleanField, default True)
- `custom_selectors` — CSS selectors to strip before extraction, one per line
- `fetch_config` — JSONField for Playwright options (`wait_for_selector`, `sleep_seconds`, `dismiss_selectors`)
- Unique together: `(organization, document_type, url)`

#### Document Types
`tos`, `privacy`, `cookie`, `refund`, `childrens_privacy`, `subscription`, `service_agreement`, `service_fees`, `user_agreement`, `conduct`, `acceptable_use`, `dmca`, `payment_service`, `other`

### `DocumentSnapshot`
- FK to `Document` (CASCADE)
- `captured_at` (auto_now_add)
- `cleaned_text` — markdown-like plain text
- `text_hash` — SHA-256 of `cleaned_text` (indexed); used for deduplication

---

## Core Service Layer (`monitor/services.py`)

All functions are **pure and side-effect-free** — callable from management commands, or views.

### Key functions

| Function | Purpose |
|---|---|
| `fetch_html(url)` | HTTP GET via `requests`; respects per-domain rate limiting (1 req/s) |
| `fetch_html_playwright(url, ...)` | Headless Chromium fetch for JS-rendered pages |
| `fetch_pdf_bytes(url)` | Fetches raw bytes; detects PDF by Content-Type or `.pdf` extension |
| `extract_pdf_text(pdf_bytes)` | Extracts text from PDF using pdfplumber |
| `extract_text(html, extra_selectors)` | Cleans HTML → markdown-like text (see below) |
| `clean_html(html)` | Backwards-compatible alias for `extract_text` |
| `compute_hash(text)` | SHA-256 hex digest |
| `create_snapshot_if_changed(doc, text)` | Saves snapshot only if hash differs from last; updates `last_checked`/`last_changed` |
| `fetch_document_content(doc)` | Dispatches to correct fetch method; raises `NotImplementedError` for unimplemented methods |
| `fetch_and_snapshot(doc)` | Full pipeline: fetch → extract → snapshot; returns `(snapshot, created)` |

### `extract_text` behaviour
- Strips: `script`, `style`, `noscript`, `head`, `nav`, `footer`, `iframe`, `aside`, `form`
- Strips any additional CSS selectors passed via `extra_selectors` (from `Document.custom_selectors`)
- Prefers `<main>` or `<article>` as content root when present
- Converts: `h1–h6` → `# … ######`, `strong/b` → `**…**`, `em/i` → `_…_`, `li` → `- `, `hr` → `---`
- Aggressively normalises whitespace (collapses blank lines, internal spaces)

---

## Tasks (`monitor/tasks.py`)

Plain synchronous functions — no Celery, no Redis:

- **`check_document(doc_id)`** — fetches one document and snapshots it if changed; returns a result dict; calls `dispatch_change_notifications` when a new snapshot is created; handles missing/inactive/failing documents and `NotImplementedError`
- **`send_change_notifications(document_id, new_snapshot_id)`** — queues a `PendingNotification` for every subscriber (daily & weekly alike; no immediate emails)
- **`send_daily_digests()` / `send_weekly_digests()`** — send one digest email per user (links + per-document unsubscribe), then clear that user's queue
- **`is_weekly_digest_day(now=None)`** — True when the given date's ISO weekday equals `WEEKLY_DIGEST_WEEKDAY` (default Monday)

### Daily cycle

The daily cron job (`render.yaml`) runs `python manage.py fetch_documents`, which fetches every active document, queues change notifications, sends the **daily** digests, and sends the **weekly** digests whenever `is_weekly_digest_day()` is True. There is no separate beat scheduler.

---

## Management Command

```bash
python manage.py fetch_documents                  # full daily cycle (fetch → queue → digests)
python manage.py fetch_documents --id 3           # fetch single document by PK
python manage.py fetch_documents --dry-run        # list without fetching
python manage.py fetch_documents --skip-digests   # fetch + queue notifications only
python manage.py fetch_documents --skip-notifications  # fetch only
```

---

## Views & URLs

All views are **class-based** (`ListView`, `DetailView`).

| URL pattern | View | Name |
|---|---|---|
| `/` | `RecentChangesView` | `monitor:home` |
| `/document/<pk>/` | `DocumentDetailView` | `monitor:document_detail` |
| `/document/<pk>/diff/<old_pk>/<new_pk>/` | `SnapshotDiffView` | `monitor:snapshot_diff` |
| `/document/<pk>/snapshot/<snap_pk>/` | `SnapshotTextView` | `monitor:snapshot_text` |

- Homepage shows snapshots from the **last 14 days**, paginated by 20
- Diff view uses `difflib.HtmlDiff` for side-by-side contextual diffs
- Snapshot text view guards that the snapshot belongs to the document in the URL

---

## Admin Configuration

- **`CountryAdmin`** — searchable by name/code
- **`LanguageAdmin`** — searchable by name/code
- **`OrganizationAdmin`** — autocomplete for `parent` field; shows document count; clickable website URL
- **`DocumentAdmin`** — fieldsets, inline snapshots (preview), list filters by type/language/country/method/active/org
- **`DocumentSnapshotAdmin`** — read-only; filterable by org and document type

---

## Configuration & Environment Variables

All secrets and connection strings are read from environment variables. See `.env.example` for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | insecure dev key | Django secret key |
| `DJANGO_DEBUG` | `true` | Debug mode |
| `DJANGO_ALLOWED_HOSTS` | `*` (debug) | Comma-separated allowed hosts |
| `DB_ENGINE` | `sqlite3` | `sqlite3` or `postgresql` |
| `DB_NAME/USER/PASSWORD/HOST/PORT` | — | PostgreSQL connection |
| `WEEKLY_DIGEST_WEEKDAY` | `1` | ISO weekday the weekly digests are sent (default Monday) |
| `MAILGUN_API_KEY` / `MAILGUN_DOMAIN` | _(empty → console)_ | Transactional mail via the Mailgun HTTP API |

**Never commit `.env` files.** Use `.env.example` as a template.

---

## Docker

```bash
# Start the web service and its database
docker-compose up --build

# Run the daily monitoring cycle (fetch → queue → digests)
docker-compose run --rm web python manage.py fetch_documents
```

Services: `db` (postgres:16-alpine), `web` (gunicorn). The daily cycle is a
single synchronous command — schedule it with your platform's cron (see
`render.yaml`) rather than extra containers.

---

## Development Practices

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
2. Keep it side-effect-free and mockable (no external HTTP/filesystem calls without a seam)
3. Add unit tests with `unittest.mock.patch` for external calls (HTTP, filesystem)

---

## Testing

```bash
python manage.py test monitor              # run all tests
python manage.py test monitor --verbosity=2  # verbose output
```

### Test conventions
- Use `django.test.TestCase` for all tests
- Mock all external HTTP calls with `@patch("monitor.services.requests.get")` or `@patch("monitor.services.fetch_pdf_bytes")`
- Use `get_or_create` when referencing seeded data (e.g. the English language seeded by migration 0003)
- Group tests by class: one `TestCase` subclass per model/service/view area
- Aim for ≥ 80% coverage (enforced in CI)

---

## CI/CD (GitHub Actions)

Three jobs run on every push/PR to `main`/`master`:

1. **test** — Python 3.12 & 3.13 matrix; runs migrations, tests, and coverage (≥ 80% required)
2. **security** — `pip-audit --strict` for known dependency vulnerabilities
3. **lint** — `ruff check` + `ruff format --check`

All three jobs must pass before merging.

---

## Code Style

- Follow **Ruff** rules (configured in `ruff.toml`)
- Use **type hints** on all function signatures in `services.py` and `views.py`
- Use `format_html()` for any HTML in admin methods — never use string concatenation
- Keep `services.py` functions pure; no Django ORM calls except in `create_snapshot_if_changed` and `fetch_and_snapshot`
- Template URLs must use `{% url 'monitor:name' %}` (namespaced)
- No hardcoded secrets anywhere in the codebase

---

## Future Work (planned, not yet implemented)

- User login / authentication (Django auth is already installed)
- Playwright fetch support (infrastructure exists; `fetch_document_content` raises `NotImplementedError` for now)
- Email/webhook notifications on document changes
- Organisation/document search and filtering on the public UI
- API endpoints for programmatic access
