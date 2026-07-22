"""Retention pruning — bounded table growth for a pipeline that runs forever.

Terminal jobs are never deleted by the pipeline itself, so rows (and their
imported `PropertyRecord`s and `OutboxEvent`s) accumulate without bound. The
Beat-scheduled `jobs.prune_expired` task deletes terminal jobs older than
``RETENTION_DAYS`` — a `Job` delete CASCADEs to its records and events
(`jobs/models.py`) — then prunes aged DISPATCHED outbox rows whose job is still
alive (e.g. redriven since). PENDING outbox rows are never touched: an
undispatched event is work, not history.

Env-gated **off** by default (``RETENTION_DAYS=0``). Deletes run in
``RETENTION_BATCH_SIZE`` pk-slices so no single transaction holds a giant
multi-table cascade. The DB-derived terminal counters shrink at the prune
horizon — the documented monotonicity boundary in ADR 0006; alert on
``increase()``/``rate()``, never absolute totals.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Model, QuerySet
from django.utils import timezone

from .models import Job, OutboxEvent

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = (Job.Status.SUCCEEDED, Job.Status.FAILED, Job.Status.DEAD_LETTER)


@shared_task(name="jobs.prune_expired")
def prune_expired() -> dict:
    """Delete aged terminal jobs (+ cascades) and stale DISPATCHED outbox rows.

    No-op unless ``RETENTION_DAYS`` > 0. Returns the top-level rows deleted per
    model (cascaded records/events ride along with their job).
    """
    if settings.RETENTION_DAYS <= 0:
        return {"jobs": 0, "outbox_events": 0}

    cutoff = timezone.now() - timedelta(days=settings.RETENTION_DAYS)
    jobs_deleted = _delete_batched(
        Job.objects.filter(status__in=TERMINAL_STATUSES, finished_at__lt=cutoff)
    )
    outbox_deleted = _delete_batched(
        OutboxEvent.objects.filter(status=OutboxEvent.Status.DISPATCHED, dispatched_at__lt=cutoff)
    )
    if jobs_deleted or outbox_deleted:
        logger.info(
            "retention.pruned",
            extra={
                "jobs": jobs_deleted,
                "outbox_events": outbox_deleted,
                "cutoff": cutoff.isoformat(),
            },
        )
    return {"jobs": jobs_deleted, "outbox_events": outbox_deleted}


def _delete_batched[M: Model](queryset: QuerySet[M]) -> int:
    """Delete the queryset's rows in pk-sliced batches; return top-level rows deleted.

    Re-queries each round, so it terminates once the filter matches nothing;
    counting from delete()'s per-model breakdown excludes cascaded children.
    """
    model = queryset.model
    label = model._meta.label
    deleted = 0
    while True:
        batch = list(queryset.values_list("pk", flat=True)[: settings.RETENTION_BATCH_SIZE])
        if not batch:
            return deleted
        _, per_model = model._default_manager.filter(pk__in=batch).delete()
        deleted += per_model.get(label, 0)
