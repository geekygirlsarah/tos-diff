# TosDiff

A Django application that monitors changes to Terms of Service and Privacy Policies across multiple websites. It periodically fetches documents, extracts clean text, and shows a diff whenever content changes.

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Quick Start (Docker)](#quick-start-docker)
- [Local Development (without Docker)](#local-development-without-docker)
- [Environment Variables](#environment-variables)
- [Management Commands](#management-commands)
- [Running Tests](#running-tests)
- [Deployment](#deployment)
- [CI/CD](#cicd)

---

## Features

- Track **Terms of Service**, **Privacy Policies**, and **Cookie Policies** for any website
- Automatic periodic fetching via a **daily cron job** (runs `python manage.py fetch_documents`)
- SHA-256 hash-based **duplicate detection** — snapshots are only saved when content actually changes
- **Markdown-like text extraction** from HTML (headings, bold, italic, lists) suitable for clean diffs
- **Side-by-side diff view** between any two snapshots using Python's `difflib`
- Per-document **custom CSS selectors** to exclude cookie banners, sidebars, etc.
- Django Admin for managing organisations and documents
- Bootstrap 5 responsive UI

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | Django 6.0+ |
| Database | PostgreSQL 16 |
| Task scheduling | Daily cron job (`fetch_documents`); no Celery/Redis |
| HTML parsing | BeautifulSoup4 + lxml |
| Web server | Gunicorn |
| Containers | Docker + Docker Compose |

---

## Project Structure

```
tosdiff/            # Django project package (settings, urls)
monitor/            # Main app — models, views, services, tasks, admin
  management/
    commands/       # fetch_documents management command
  migrations/
templates/          # HTML templates (base + monitor/)
Dockerfile
docker-compose.yml
docker-entrypoint.sh
.env.example
pyproject.toml
```

---

## Quick Start (Docker)

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) ≥ 24
- [Docker Compose](https://docs.docker.com/compose/) ≥ 2.20 (included with Docker Desktop)

### 1. Clone and configure

```bash
git clone <repo-url>
cd TosDiff-New
cp .env.example .env
```

Edit `.env` and set at minimum:

```dotenv
DJANGO_SECRET_KEY=<generate with command below>
DB_PASSWORD=<strong password>
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
```

Generate a secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### 2. Build and start all services

```bash
docker compose up --build -d
```

This starts the web service and its database:

| Service | Role |
|---|---|
| `db` | PostgreSQL database |
| `web` | Django + Gunicorn (port 8000) |

Migrations run automatically when `web` starts.

The daily monitoring cycle is not run by Docker Compose — trigger it on demand
with `docker compose exec web python manage.py fetch_documents` (or schedule it
via your platform's cron, e.g. Render's cron jobs in `render.yaml`).

### 3. Create a superuser

```bash
docker compose exec web python manage.py createsuperuser
```

### 4. Open the app

- Homepage: http://localhost:8000
- Admin: http://localhost:8000/admin

### 5. Stop services

```bash
docker compose down          # stop containers, keep volumes
docker compose down -v       # stop and delete all data volumes
```

---

## Local Development (without Docker)

### Prerequisites

- Python 3.12+
- PostgreSQL (or use SQLite for quick local testing)

### Setup

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install django requests beautifulsoup4 lxml "psycopg[binary]" gunicorn

# SQLite is used by default — no DB_ENGINE needed
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

To use PostgreSQL locally, set the environment variables before running:

```bash
export DB_ENGINE=postgresql
export DB_NAME=tosdiff
export DB_USER=tosdiff
export DB_PASSWORD=yourpassword
export DB_HOST=localhost
python manage.py migrate
```

### Running the daily cycle manually

```bash
# Full cycle: fetch → queue change notifications → send daily digests
# (weekly digests go out on WEEKLY_DIGEST_WEEKDAY, default Monday)
python manage.py fetch_documents
```

There is no worker process — the command does everything synchronously in one
process, so schedule it with a plain cron job (e.g. Render's cron service, see
`render.yaml`).

---

## Environment Variables

All variables are read from the environment (or `.env` when using Docker Compose).

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | insecure dev key | Django secret key — **must be changed in production** |
| `DJANGO_DEBUG` | `true` | Set to `false` in production |
| `DJANGO_ALLOWED_HOSTS` | `*` (when DEBUG) | Comma-separated list of allowed hostnames |
| `DB_ENGINE` | `sqlite3` | `sqlite3` or `postgresql` |
| `DB_NAME` | `tosdiff` | PostgreSQL database name |
| `DB_USER` | `tosdiff` | PostgreSQL user |
| `DB_PASSWORD` | _(required for postgres)_ | PostgreSQL password |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `GUNICORN_WORKERS` | `3` | Number of Gunicorn worker processes |
| `GUNICORN_TIMEOUT` | `120` | Gunicorn worker timeout (seconds) |
| `WEB_PORT` | `8000` | Host port mapped to the web container |
| `BASE_URL` | `http://localhost:8000` | Absolute base URL for links in emails (login codes, change digests) |
| `MAILGUN_API_KEY` | _(empty → console backend)_ | Mailgun HTTP API key for transactional mail |
| `MAILGUN_DOMAIN` | _(empty)_ | Mailgun sending domain (e.g. `mg.example.com`) |
| `DEFAULT_FROM_EMAIL` | `TosDiff <no-reply@...>` | From address for digests — must be on `MAILGUN_DOMAIN` |
| `WEEKLY_DIGEST_WEEKDAY` | `1` | ISO weekday (1=Monday … 7=Sunday) the weekly digests are sent by the daily cron |
| `SPONSOR_GITHUB_URL` | _(empty)_ | GitHub Sponsors link for the footer's "Support TosDiff" buttons (hidden when empty) |
| `SPONSOR_KOFI_URL` | _(empty)_ | Ko-Fi link for the footer's "Support TosDiff" buttons (hidden when empty) |

---

## Management Commands

### Fetch documents

```bash
# Fetch all active documents synchronously
python manage.py fetch_documents

# Fetch a single document by primary key
python manage.py fetch_documents --id 3

# Dry run — list what would be fetched without fetching
python manage.py fetch_documents --dry-run

# Full cycle but skip digest emails (fetch + queue notifications only)
python manage.py fetch_documents --skip-digests

# Full cycle but skip the change-notification queueing
python manage.py fetch_documents --skip-notifications

# Via Docker
docker compose exec web python manage.py fetch_documents
docker compose exec web python manage.py fetch_documents --id 3
```

---

## Running Tests

```bash
# Local
python manage.py test monitor --verbosity=2

# With coverage
coverage run manage.py test monitor
coverage report

# Via Docker
docker compose exec web python manage.py test monitor --verbosity=2
```

---

## Deployment

### General checklist

1. Set `DJANGO_DEBUG=false`
2. Set a strong, unique `DJANGO_SECRET_KEY`
3. Set `DJANGO_ALLOWED_HOSTS` to your domain(s)
4. Use a strong `DB_PASSWORD`
5. Put Gunicorn behind a reverse proxy (nginx, Caddy, etc.) that handles TLS
6. Serve `staticfiles/` from the reverse proxy or a CDN

### Example: deploy on a VPS with nginx

```nginx
server {
    listen 80;
    server_name example.com;

    location /static/ {
        alias /path/to/staticfiles/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then run with Docker Compose on the server:

```bash
docker compose pull   # if using a registry
docker compose up -d --build
```

### Scheduling the daily fetch

`fetch_documents` runs the whole monitoring cycle synchronously — fetches every
active document, queues change notifications for subscribers, sends the daily
digests, and sends the weekly digests on `WEEKLY_DIGEST_WEEKDAY`. Run it from a
plain cron job; the Render configuration lives in `render.yaml` (web service +
a `fetch-documents` cron service).

---

## CI/CD

GitHub Actions runs three jobs on every push and pull request to `main`/`master`:

| Job | What it does |
|---|---|
| **test** | Runs the full test suite with coverage (Python 3.12 & 3.13); fails if coverage < 80% |
| **security** | Runs `pip-audit` to check for known dependency vulnerabilities |
| **lint** | Runs `ruff check` and `ruff format --check` |

See [`.github/workflows/ci.yml`](.github/workflows/ci.yml) for the full configuration.
