"""Expose the Celery app at import time so `@shared_task` autodiscovery works."""

from .celery import app as celery_app

__version__ = "0.7.0"  # x-release-please-version

__all__ = ("__version__", "celery_app")
