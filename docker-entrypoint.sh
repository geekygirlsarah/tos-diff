#!/bin/sh
set -e

# Wait for PostgreSQL to be ready (only when using postgres engine)
if [ "$DB_ENGINE" = "postgresql" ]; then
    echo "Waiting for PostgreSQL at $DB_HOST:$DB_PORT..."
    until python -c "
import sys, psycopg
try:
    psycopg.connect(
        host='$DB_HOST', port='$DB_PORT',
        dbname='$DB_NAME', user='$DB_USER', password='$DB_PASSWORD',
        connect_timeout=2,
    ).close()
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
        exec gunicorn tosdiff_new.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "${GUNICORN_WORKERS:-3}" \
            --timeout "${GUNICORN_TIMEOUT:-120}" \
            --access-logfile - \
            --error-logfile -
        ;;
    worker)
        echo "Starting Celery worker..."
        exec celery -A tosdiff_new worker \
            --loglevel="${CELERY_LOG_LEVEL:-info}" \
            --concurrency="${CELERY_CONCURRENCY:-2}"
        ;;
    beat)
        echo "Starting Celery Beat scheduler..."
        exec celery -A tosdiff_new beat \
            --loglevel="${CELERY_LOG_LEVEL:-info}" \
            --scheduler django_celery_beat.schedulers:DatabaseScheduler 2>/dev/null \
            || exec celery -A tosdiff_new beat \
                --loglevel="${CELERY_LOG_LEVEL:-info}"
        ;;
    manage)
        shift
        exec python manage.py "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
