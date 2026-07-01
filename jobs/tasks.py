"""Celery tasks for the jobs app: the outbox relay, the worker, and recovery.

`dispatch_outbox` (Beat) claims PENDING outbox rows and publishes one `process_job`
message each, then marks them DISPATCHED. `process_job` ingests the job's CSV into
PropertyRecords and drives it to a terminal state — retrying transient failures with
capped, jittered backoff and dead-lettering once attempts are exhausted. `recover_jobs`
(Beat) reaps expired leases (crashed workers) and re-dispatches jobs whose backoff elapsed.

Retry/lease state lives in Postgres (Job.attempts / available_at / leased_until /
lease_token), never the broker, so it stays queryable and survives a broker restart.
The `lease_token` fences a reclaimed-then-resumed worker's stale write (see `_fenced_update`).
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Model, QuerySet
from django.utils import timezone

from .ingest import IngestError, load_csv_text, parse_rows
from .models import Job, OutboxEvent, PropertyRecord

logger = logging.getLogger(__name__)

OUTBOX_BATCH_SIZE = 100
PROGRESS_CHUNK = 50


def _log(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured log event; `fields` become top-level JSON keys."""
    logger.log(level, event, extra=fields)


def _log_job(event: str, job: Job, *, level: int = logging.INFO, **fields: Any) -> None:
    """Structured job event, auto-tagging job_id and attempts."""
    _log(event, level=level, job_id=job.id, attempts=job.attempts, **fields)


def _ms(started: float) -> int:
    """Elapsed milliseconds since a `time.monotonic()` mark."""
    return round((time.monotonic() - started) * 1000)


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
    """Ingest a job's CSV and drive it to a terminal state.

    PENDING → PROCESSING → SUCCEEDED, or on failure either FAILED (permanent — poison
    input that can never succeed) or, for a transient error, a backoff retry until
    `JOB_MAX_ATTEMPTS` is reached and the job is dead-lettered. A non-PENDING job is a
    no-op, so a redelivered message never reprocesses.
    """
    started = time.monotonic()
    job = _claim_pending(job_id)
    if job is None:
        return "skipped"

    try:
        result = _import_properties(job)
    except IngestError as exc:
        _terminal(job, Job.Status.FAILED, error=str(exc))
        _log_job("job.failed", job, error_class=type(exc).__name__, error=str(exc))
        return "failed"
    except Exception as exc:  # noqa: BLE001 — transient: retry with backoff or dead-letter
        return _handle_transient(job, exc)

    _terminal(job, Job.Status.SUCCEEDED, progress=100, result=result)
    _log_job("job.succeeded", job, latency_ms=_ms(started), rows_imported=result["rows_imported"])
    return "succeeded"


@shared_task(name="jobs.recover_jobs")
def recover_jobs() -> dict:
    """Recover stuck or scheduled jobs (Beat-scheduled).

    Two lanes: reap expired leases (jobs whose worker died mid-process) back into the
    retry flow, then re-dispatch jobs whose backoff has elapsed. Reaping first means a
    just-reclaimed job (available_at=now) is re-dispatched in the same tick.
    """
    return {"reaped": _reap_expired_leases(), "requeued": _requeue_due_retries()}


def _claim_pending(job_id: str) -> Job | None:
    """Atomically claim a PENDING job: flip it to PROCESSING under a fresh lease.

    Returns the claimed Job — so the caller reads attempts/lease_token without a
    re-query — or None if the job was already taken or is no longer PENDING.
    """
    with transaction.atomic():
        job = _lock_for_claim(Job.objects.filter(pk=job_id)).first()
        if job is None or job.status != Job.Status.PENDING:
            return None
        job.status = Job.Status.PROCESSING
        job.attempts += 1
        job.leased_until = timezone.now() + timedelta(seconds=settings.JOB_LEASE_SECONDS)
        job.lease_token = uuid.uuid4()
        job.available_at = None
        job.save(
            update_fields=[
                "status",
                "attempts",
                "leased_until",
                "lease_token",
                "available_at",
                "updated_at",
            ]
        )
        _log_job("job.claimed", job)
        return job


def _handle_transient(job: Job, exc: Exception) -> str:
    """Schedule a backoff retry, or dead-letter once attempts are exhausted."""
    if job.attempts >= settings.JOB_MAX_ATTEMPTS:
        _terminal(job, Job.Status.DEAD_LETTER, error=str(exc))
        _log_job("job.dead_letter", job, level=logging.WARNING, error_class=type(exc).__name__)
        return "dead_letter"

    delay = _retry_delay(job.attempts)
    _fenced_update(
        job,
        status=Job.Status.PENDING,
        available_at=timezone.now() + timedelta(seconds=delay),
        leased_until=None,
        lease_token=None,
        progress=0,
        error=str(exc),
    )
    _log_job("job.retry_scheduled", job, retry_in_s=round(delay, 1))
    return "retry"


def _terminal(
    job: Job,
    status: str,
    *,
    progress: int | None = None,
    result: dict | None = None,
    error: str = "",
) -> None:
    """Move the job to a terminal state, releasing the lease (fenced write)."""
    fields: dict = {
        "status": status,
        "leased_until": None,
        "lease_token": None,
        "available_at": None,
        "error": error,
    }
    if progress is not None:
        fields["progress"] = progress
    if result is not None:
        fields["result"] = result
    _fenced_update(job, **fields)


def _fenced_update(job: Job, **fields: Any) -> int:
    """Write `fields` to the job only while it is still PROCESSING under our token.

    The status + lease_token guard fences a reclaimed-then-resumed slow worker: once
    the reaper has handed the job to someone else, this stale write matches zero rows
    and is silently discarded instead of clobbering the row.
    """
    fields["updated_at"] = timezone.now()
    return Job.objects.filter(
        pk=job.id,
        status=Job.Status.PROCESSING,
        lease_token=job.lease_token,
    ).update(**fields)


def _retry_delay(attempts: int) -> float:
    """Exponential backoff with full jitter, capped at JOB_RETRY_MAX_SECONDS.

    `attempts` is the count already made (>=1), so the ceiling doubles each time.
    Full jitter (uniform 0..ceiling) spreads simultaneous failures across the window
    instead of retrying them in lockstep.
    """
    ceiling = min(
        settings.JOB_RETRY_MAX_SECONDS,
        settings.JOB_RETRY_BASE_SECONDS * (2 ** (attempts - 1)),
    )
    return random.uniform(0, ceiling)


def _import_properties(job: Job) -> dict:
    records, errors = parse_rows(load_csv_text(job.payload))
    _bulk_create_with_progress(job, records)
    return {
        "rows_total": len(records) + len(errors),
        # On an idempotent re-run this is the job's target state (these rows exist),
        # not the number newly inserted — the right semantic for at-least-once delivery.
        "rows_imported": len(records),
        "rows_skipped": len(errors),
        "errors": errors,
    }


def _bulk_create_with_progress(job: Job, records: list[dict]) -> None:
    total = len(records)
    for start in range(0, total, PROGRESS_CHUNK):
        batch = records[start : start + PROGRESS_CHUNK]
        # ignore_conflicts: a redelivered job re-imports the same rows; the unique
        # (job, external_id) constraint turns each duplicate insert into a no-op,
        # giving exactly-once *effect* without re-reading what already landed.
        PropertyRecord.objects.bulk_create(
            [PropertyRecord(job=job, **row) for row in batch], ignore_conflicts=True
        )
        done = start + len(batch)
        Job.objects.filter(pk=job.id).update(
            progress=int(done / total * 100), updated_at=timezone.now()
        )


def _requeue_due_retries() -> int:
    """Dispatch `process_job` for each PENDING job whose backoff has elapsed.

    Keyed on `available_at` (set only on a scheduled retry), so this lane never
    touches a brand-new job — those have `available_at IS NULL` and belong to the
    outbox. Before publishing, push `available_at` forward by a visibility window so a
    job isn't re-dispatched every tick while it waits to be claimed; the claim clears
    it, and a lost message simply becomes due again after the window (self-healing).
    """
    now = timezone.now()
    requeued = 0
    with transaction.atomic():
        due = _lock_for_claim(
            Job.objects.filter(
                status=Job.Status.PENDING,
                available_at__isnull=False,
                available_at__lte=now,
            ).order_by("available_at")
        )
        for job in list(due[:OUTBOX_BATCH_SIZE]):
            Job.objects.filter(pk=job.id).update(
                available_at=now + timedelta(seconds=settings.JOB_REQUEUE_VISIBILITY_SECONDS),
                updated_at=now,
            )
            process_job.delay(str(job.id))
            requeued += 1
    if requeued:
        _log("recover.requeued", count=requeued)
    return requeued


def _reap_expired_leases() -> int:
    """Reclaim jobs whose lease expired — their worker died mid-process.

    The PENDING-guard blocks the broker from redelivering a PROCESSING job (never two
    live workers on one), so a crashed worker's job is recoverable only here. Treat it
    as a transient failure: requeue for immediate retry, or dead-letter if attempts are
    spent. The crashed attempt already consumed its increment at claim, so attempts is
    left untouched.
    """
    reaped = 0
    with transaction.atomic():
        stale = _lock_for_claim(
            Job.objects.filter(status=Job.Status.PROCESSING, leased_until__lt=timezone.now())
        )
        for job in list(stale[:OUTBOX_BATCH_SIZE]):
            _recover_one_lease(job)
            reaped += 1
    if reaped:
        _log("recover.reaped", level=logging.WARNING, count=reaped)
    return reaped


def _recover_one_lease(job: Job) -> None:
    """Dead-letter a lease-expired job if its attempts are spent, else requeue it now."""
    if job.attempts >= settings.JOB_MAX_ATTEMPTS:
        _terminal(job, Job.Status.DEAD_LETTER, error="lease expired")
        _log_job("job.dead_letter", job, level=logging.WARNING, reason="lease expired")
    else:
        _fenced_update(
            job,
            status=Job.Status.PENDING,
            available_at=timezone.now(),
            leased_until=None,
            lease_token=None,
            progress=0,
            error="lease expired",
        )


def _lock_for_claim[M: Model](queryset: QuerySet[M]) -> QuerySet[M]:
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
