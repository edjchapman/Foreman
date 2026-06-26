"""Expose the Celery app at import time so `@shared_task` autodiscovery works."""

from .celery import app as celery_app

__all__ = ("celery_app",)
