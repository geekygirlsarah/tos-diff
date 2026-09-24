#!/bin/sh
set -e

# Wait for PostgreSQL to be ready (only when using postgres engine)
if [ "$DB_ENGINE" = "postgresql" ]; then
    echo "Waiting for PostgreSQL to be ready..."
    until python -c "
import os, sys, psycopg
from urllib.parse import urlparse
url = os.environ.get('DATABASE_URL')
if url:
    parts = urlparse(url)
    kwargs = {
        'host': parts.hostname or 'localhost',
        'port': parts.port or 5432,
        'dbname': parts.path.lstrip('/'),
        'user': parts.username or '',
        'password': parts.password or '',
    }
else:
    kwargs = {
        'host': os.environ.get('DB_HOST', 'localhost'),
        'port': os.environ.get('DB_PORT', 5432),
        'dbname': os.environ.get('DB_NAME', 'tosdiff'),
        'user': os.environ.get('DB_USER', 'tosdiff'),
        'password': os.environ.get('DB_PASSWORD', ''),
    }
try:
    psycopg.connect(connect_timeout=2, **kwargs).close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
        sleep 1
    done
    echo "PostgreSQL is ready."
fi

case "$1" in
    web)
        echo "Running migrations..."
        python manage.py migrate --noinput
        echo "Starting Gunicorn..."
        exec gunicorn tosdiff.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "${GUNICORN_WORKERS:-3}" \
            --timeout "${GUNICORN_TIMEOUT:-120}" \
            --access-logfile - \
            --error-logfile -
        ;;
    manage)
        shift
        exec python manage.py "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
