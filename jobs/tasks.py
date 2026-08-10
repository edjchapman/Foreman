"""Celery tasks for the jobs app: the outbox relay, the worker, and recovery.

`dispatch_outbox` (Beat) claims PENDING outbox rows and publishes one `process_job`
message each, then marks them DISPATCHED. `process_job` ingests the job's CSV into
PropertyRecords and drives it through the state machine in `jobs/lifecycle.py` —
every status write, its fencing, its log event, and its broadcast live there.
`recover_jobs` (Beat) reaps expired leases (crashed workers) and re-dispatches jobs
whose backoff elapsed.

This module owns message *transport* only: which broker messages get published,
and when. Publishing happens after the state machine's writes commit (the requeue
lane hands back claimed rows to publish), so a rollback can never leak a message
for un-persisted state.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from config.otel import get_tracer, span_from_carrier

from . import lifecycle
from .faults import is_fault_source, load_fault_csv
from .ingest import IngestError, load_csv_text, parse_rows
from .models import Job, OutboxEvent, PropertyRecord

# Celery autodiscovery imports only `<app>.tasks` — re-export the retention task
# so it registers with the worker (jobs/retention.py is its own module to keep
# this file under the size limit).
from .retention import prune_expired  # noqa: F401

logger = logging.getLogger(__name__)

OUTBOX_BATCH_SIZE = 100
PROGRESS_CHUNK = 50


class _FencedOutError(Exception):
    """Raised mid-import when a progress write is fenced: this worker lost its lease."""


def _log(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured log event; `fields` become top-level JSON keys."""
    logger.log(level, event, extra=fields)


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
        pending = (
            OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING)
            .order_by("id")
            .lock_for_claim()
        )
        for event in pending[:OUTBOX_BATCH_SIZE]:
            # Re-hydrate this event's own trace context (a batch mixes many requests, so
            # ambient context would mislink). Starting the span current means Celery's
            # instrumentation injects it into the process_job message — the worker span
            # then links back to the original API request. See ADR 0008.
            with span_from_carrier("outbox.dispatch", event.payload.get("trace")):
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
    no-op, so a redelivered message never reprocesses; a fenced-out write (this worker
    was reaped mid-run) returns "fenced" without touching the new owner's row.
    """
    job = lifecycle.claim(job_id)
    if job is None:
        return "skipped"

    try:
        with get_tracer().start_as_current_span("ingest"):
            result = _import_properties(job)
    except IngestError as exc:
        lifecycle.fail(job, error=str(exc), error_class=type(exc).__name__)
        return "failed"
    except _FencedOutError:
        return "fenced"
    except Exception as exc:  # noqa: BLE001 — transient: retry with backoff or dead-letter
        return lifecycle.retry_or_dead_letter(job, error=str(exc), error_class=type(exc).__name__)

    return "succeeded" if lifecycle.succeed(job, result=result) else "fenced"


@shared_task(name="jobs.recover_jobs")
def recover_jobs() -> dict:
    """Recover stuck or scheduled jobs (Beat-scheduled).

    Two lanes: reap expired leases (jobs whose worker died mid-process) back into the
    retry flow, then re-dispatch jobs whose backoff has elapsed. Reaping first means a
    just-reclaimed job (available_at=now) is re-dispatched in the same tick.
    """
    reaped = lifecycle.reap_expired_leases()
    if reaped:
        _log("recover.reaped", level=logging.WARNING, count=reaped)

    due = lifecycle.claim_due_for_requeue()
    for job in due:
        process_job.delay(str(job.id))
    if due:
        _log("recover.requeued", count=len(due))
    return {"reaped": reaped, "requeued": len(due)}


def _import_properties(job: Job) -> dict:
    source = str(job.payload.get("source", ""))
    # Demo fault sources raise a transient error while "active" (exercising the retry /
    # dead-letter path) and otherwise resolve to the sample CSV; see jobs/faults.py.
    text = (
        load_fault_csv(source, attempts=job.attempts, created_at=job.created_at)
        if is_fault_source(source)
        else load_csv_text(job.payload)
    )
    records, errors = parse_rows(text)
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
        if not lifecycle.record_progress(job, int(done / total * 100)):
            # Fenced out: the reaper reassigned this job — stop; the new owner's
            # re-import converges on the same rows (ignore_conflicts).
            raise _FencedOutError
