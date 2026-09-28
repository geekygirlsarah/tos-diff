FROM python:3.12-slim

# Keeps Python from generating .pyc files and enables stdout/stderr logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# uv resolves the dependency set from pyproject.toml + uv.lock, so the image
# can never drift from the versions the tests and the security audit ran
# against. The virtualenv lives outside /app so `COPY . .` cannot clobber it.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install system dependencies needed by psycopg (libpq) and lxml. Git is needed
# at runtime so the footer can show the "Last updated" date from the last commit
# (the .git directory is copied in with the source below).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Pinned uv version, matching CI (see .github/workflows/ci.yml).
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

# Install the locked dependencies first. --no-install-project skips installing
# the project itself, which is not copied yet; this keeps the layer cached so
# ordinary source edits do not re-resolve dependencies. Only edits to
# pyproject.toml / uv.lock invalidate it. Note the django floor now comes from
# pyproject (>=6.1) — the previous hardcoded `django>=6.0.5` could resolve to a
# version without MAILERS / AdminEmailHandler(using=...) and break the boot.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

# Install Playwright's Chromium browser and its system libraries (--with-deps
# runs apt-get for the shared libraries browsers need at runtime, e.g. libgtk-3).
# Deliberately placed BEFORE `COPY . .`: playwright is a runtime dependency, so
# this layer is already satisfied by the dependency sync above and the ~175MB
# browser download is cached across ordinary source edits instead of re-running
# on every one of them.
RUN playwright install --with-deps chromium

# Copy project source and install the project into the virtualenv.
COPY . .
RUN uv sync --no-dev

# Collect static files (requires DJANGO_SETTINGS_MODULE to be set at build time
# or via --build-arg; harmless if STATIC_ROOT doesn't exist yet)
RUN DJANGO_SECRET_KEY=build-placeholder \
    DB_ENGINE=sqlite3 \
    python manage.py collectstatic --noinput

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["web"]
