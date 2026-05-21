"""Celery application for TosDiff."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tosdiff_new.settings")

app = Celery("tosdiff_new")

# Load config from Django settings, using the CELERY_ namespace
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all installed apps
app.autodiscover_tasks()
