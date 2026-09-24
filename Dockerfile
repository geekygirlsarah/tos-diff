FROM python:3.12-slim

# Keeps Python from generating .pyc files and enables stdout/stderr logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies needed by psycopg (libpq) and lxml. Git is needed
# at runtime so the footer can show the "Last updated" date from the last commit
# (the .git directory is copied in with the source below).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer-cached unless requirements change)
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install \
        django>=6.0.5 \
        requests>=2.32 \
        beautifulsoup4>=4.12 \
        "lxml>=5.0" \
        "pdfplumber>=0.11" \
        "psycopg[binary]>=3.1" \
        "gunicorn>=22.0" \
        "playwright>=1.40"

# Install Playwright's Chromium browser and its system libraries (--with-deps
# runs apt-get for the shared libraries browsers need at runtime, e.g. libgtk-3)
RUN playwright install --with-deps chromium

# Copy project source
COPY . .

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
