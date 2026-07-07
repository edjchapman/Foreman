"""Celery application for Foreman.

The worker (`process_job`) and the outbox relay (`dispatch_outbox`, scheduled by
Celery Beat) both run against this app. Settings are read from Django with the
``CELERY_`` namespace, so broker/result config stays 12-factor in ``settings.py``.
"""

import os

from celery import Celery
from celery.signals import beat_init, worker_process_init

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("foreman")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


# Configure tracing per-process, not at import: a BatchSpanProcessor's exporter thread does
# not survive fork(), so each prefork worker child must build its own after the fork. beat
# runs in its own (unforked) process. The listener command instruments itself (see its
# handle()). Eager tests never fork, so these signals don't fire there.
@worker_process_init.connect
def _init_worker_tracing(**_kwargs: object) -> None:
    from config.otel import configure_tracing

    configure_tracing("foreman-worker")


@beat_init.connect
def _init_beat_tracing(**_kwargs: object) -> None:
    from config.otel import configure_tracing

    configure_tracing("foreman-beat")
