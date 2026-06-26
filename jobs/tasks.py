"""Celery tasks for the jobs app: the outbox relay and the worker.

`dispatch_outbox` (scheduled by Celery Beat) claims PENDING outbox rows and
publishes one `process_job` message each, then marks them DISPATCHED. `process_job`
ingests the job's CSV into PropertyRecords and drives the job to a terminal state.

Reliability hardening — retries with backoff, dead-letter, lease-based worker
idempotency — is M3. M2 relies on at-least-once delivery plus the PENDING-guard.
"""

from __future__ import annotations

from celery import shared_task
from django.db import connection, transaction
from django.utils import timezone

from .ingest import load_csv_text, parse_rows
from .models import Job, OutboxEvent, PropertyRecord

OUTBOX_BATCH_SIZE = 100
PROGRESS_CHUNK = 50


@shared_task(name="jobs.ping")
def ping() -> str:
    """Trivial task to confirm Celery autodiscovery and execution are wired."""
    return "pong"


@shared_task(name="jobs.dispatch_outbox")
def dispatch_outbox() -> int:
    """Claim PENDING outbox rows and publish a `process_job` for each.

    Returns the number dispatched. `SKIP LOCKED` (on Postgres) lets parallel
    relays claim disjoint rows without blocking; the claim+publish+mark runs in
    one transaction so a crash mid-batch leaves rows PENDING for re-dispatch.
    """
    dispatched = 0
    with transaction.atomic():
        pending = _lock_for_claim(
            OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).order_by("id")
        )
        for event in pending[:OUTBOX_BATCH_SIZE]:
            process_job.delay(event.payload["job_id"])
            event.status = OutboxEvent.Status.DISPATCHED
            event.dispatched_at = timezone.now()
            event.save(update_fields=["status", "dispatched_at"])
            dispatched += 1
    return dispatched


@shared_task(name="jobs.process_job")
def process_job(job_id: str) -> str:
    """Ingest a job's CSV and drive it PENDING → PROCESSING → SUCCEEDED|FAILED.

    Returns a short outcome string. A non-PENDING job is a no-op, so a redelivered
    message (at-least-once) does not reprocess — the *effect* stays once.
    """
    if not _claim_pending(job_id):
        return "skipped"

    try:
        result = _import_properties(job_id)
    except Exception as exc:  # noqa: BLE001 — M3 narrows this and adds retry/DLQ
        Job.objects.filter(pk=job_id).update(
            status=Job.Status.FAILED, error=str(exc), updated_at=timezone.now()
        )
        return "failed"

    Job.objects.filter(pk=job_id).update(
        status=Job.Status.SUCCEEDED,
        progress=100,
        result=result,
        error="",
        updated_at=timezone.now(),
    )
    return "succeeded"


def _claim_pending(job_id: str) -> bool:
    """Atomically flip a PENDING job to PROCESSING. Returns False if already taken."""
    with transaction.atomic():
        job = _lock_for_claim(Job.objects.filter(pk=job_id)).first()
        if job is None or job.status != Job.Status.PENDING:
            return False
        job.status = Job.Status.PROCESSING
        job.attempts += 1
        job.save(update_fields=["status", "attempts", "updated_at"])
        return True


def _import_properties(job_id: str) -> dict:
    job = Job.objects.get(pk=job_id)
    records, errors = parse_rows(load_csv_text(job.payload))
    _bulk_create_with_progress(job, records)
    return {
        "rows_total": len(records) + len(errors),
        "rows_imported": len(records),
        "rows_skipped": len(errors),
        "errors": errors,
    }


def _bulk_create_with_progress(job: Job, records: list[dict]) -> None:
    total = len(records)
    for start in range(0, total, PROGRESS_CHUNK):
        batch = records[start : start + PROGRESS_CHUNK]
        PropertyRecord.objects.bulk_create([PropertyRecord(job=job, **row) for row in batch])
        done = start + len(batch)
        Job.objects.filter(pk=job.id).update(
            progress=int(done / total * 100), updated_at=timezone.now()
        )


def _lock_for_claim(queryset):
    """Row-lock for the claim, using SKIP LOCKED where the backend supports it.

    Postgres (production) gets `select_for_update(skip_locked=True)` for concurrent,
    non-blocking claims. Backends without it (SQLite in local tests) fall back to a
    plain query — correct under the single-threaded suite; concurrency safety is a
    Postgres-runtime property exercised in CI.
    """
    features = connection.features
    if not features.has_select_for_update:
        return queryset
    if features.has_select_for_update_skip_locked:
        return queryset.select_for_update(skip_locked=True)
    return queryset.select_for_update()
