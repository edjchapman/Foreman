"""Celery application for Foreman.

The worker (`process_job`) and the outbox relay (`dispatch_outbox`, scheduled by
Celery Beat) both run against this app. Settings are read from Django with the
``CELERY_`` namespace, so broker/result config stays 12-factor in ``settings.py``.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("foreman")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
