"""The Job state machine: every legal transition and its persistence rules.

PENDING → PROCESSING (claim) → SUCCEEDED | FAILED (terminal), with transient
failures retried via PENDING (backoff or an immediate reap-requeue) until
attempts are exhausted and the job is DEAD_LETTER; redrive returns DEAD_LETTER
rows to PENDING with a fresh budget. See ADR 0002.

The module's invariant: **every client-observable Job write commits first, then
schedules its own on-commit broadcast and emits its own structured log event** —
callers never notify. Broadcasts are best-effort and at-most-once (a crash
between commit and callback loses only the frame, never state); the database row
is the durable truth. `transaction.on_commit` runs the callback immediately under
autocommit (the worker's terminal writes) and after commit inside a transaction
(claim, reap, redrive), so the one spelling is correct in both contexts.

Retry/lease state lives in Postgres (Job.attempts / available_at / leased_until /
lease_token), never the broker, so it stays queryable and survives a broker
restart. The `lease_token` fences a reclaimed-then-resumed worker's stale write
(see `_fenced_update`); a fenced-out write returns False, logs, and does not
broadcast — the live owner broadcasts its own writes.

Message transport is not this module's concern: publishing to the broker stays in
`jobs/tasks.py` (`claim_due_for_requeue` hands back rows for the caller to
publish *after* commit — a lost message just becomes due again once the
visibility window lapses).
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Job
from .realtime import notify_job

logger = logging.getLogger(__name__)

SCAN_BATCH_SIZE = 100


def claim(job_id: str) -> Job | None:
    """Atomically claim a PENDING job: flip it to PROCESSING under a fresh lease.

    Returns the claimed Job — so the caller reads attempts/lease_token without a
    re-query — or None if the job was already taken or is no longer PENDING.
    """
    with transaction.atomic():
        job = Job.objects.filter(pk=job_id).lock_for_claim().first()
        if job is None or job.status != Job.Status.PENDING:
            return None
        now = timezone.now()
        job.status = Job.Status.PROCESSING
        job.attempts += 1
        job.leased_until = now + timedelta(seconds=settings.JOB_LEASE_SECONDS)
        job.lease_token = uuid.uuid4()
        job.available_at = None
        # Stamp the start of this run; on a retry the latest claim wins, so the
        # processing histogram measures the final attempt's work (not backoff waits).
        job.started_at = now
        job.save(
            update_fields=[
                "status",
                "attempts",
                "leased_until",
                "lease_token",
                "available_at",
                "started_at",
                "updated_at",
            ]
        )
        _log_job("job.claimed", job)
        _broadcast(job)
        return job


def succeed(job: Job, *, result: dict) -> bool:
    """Fenced terminal write to SUCCEEDED; False if fenced out (no broadcast)."""
    landed = _terminal(job, Job.Status.SUCCEEDED, progress=100, result=result)
    if landed:
        _log_job(
            "job.succeeded",
            job,
            latency_ms=_processing_ms(job),
            rows_imported=result.get("rows_imported"),
        )
    return landed


def fail(job: Job, *, error: str, error_class: str = "") -> bool:
    """Fenced terminal write to FAILED (poison input — never retried)."""
    landed = _terminal(job, Job.Status.FAILED, error=error)
    if landed:
        _log_job("job.failed", job, error_class=error_class, error=error)
    return landed


def retry_or_dead_letter(
    job: Job,
    *,
    error: str,
    error_class: str = "",
    delay_seconds: float | None = None,
) -> str:
    """Schedule a backoff retry, or dead-letter once attempts are exhausted.

    ``delay_seconds=None`` applies the jittered backoff curve (`retry_delay`);
    an explicit value overrides it — the reaper passes 0 for an immediate requeue.
    Returns ``"retry"`` or ``"dead_letter"`` (the branch taken, even if the
    underlying write was fenced out — the fence is logged, not re-raised).
    """
    if job.attempts >= settings.JOB_MAX_ATTEMPTS:
        if _terminal(job, Job.Status.DEAD_LETTER, error=error):
            _log_job("job.dead_letter", job, level=logging.WARNING, error_class=error_class)
        return "dead_letter"

    delay = retry_delay(job.attempts) if delay_seconds is None else delay_seconds
    landed = _fenced_write(
        job,
        status=Job.Status.PENDING,
        available_at=timezone.now() + timedelta(seconds=delay),
        leased_until=None,
        lease_token=None,
        progress=0,
        error=error,
    )
    if landed:
        _log_job("job.retry_scheduled", job, retry_in_s=round(delay, 1))
    return "retry"


def reap_expired_leases() -> int:
    """Reclaim jobs whose lease expired — their worker died mid-process.

    The PENDING-guard in `claim` blocks the broker from redelivering a PROCESSING
    job (never two live workers on one), so a crashed worker's job is recoverable
    only here. Treat it as a transient failure: requeue for immediate retry, or
    dead-letter if attempts are spent. The crashed attempt already consumed its
    increment at claim, so attempts is left untouched.
    """
    reaped = 0
    with transaction.atomic():
        stale = Job.objects.filter(
            status=Job.Status.PROCESSING, leased_until__lt=timezone.now()
        ).lock_for_claim()
        for job in list(stale[:SCAN_BATCH_SIZE]):
            retry_or_dead_letter(
                job, error="lease expired", error_class="LeaseExpired", delay_seconds=0
            )
            reaped += 1
    return reaped


def redrive(job_ids: Iterable[str | UUID]) -> int:
    """Reset DEAD_LETTER jobs to PENDING for another run; returns the count redriven.

    ``attempts`` resets to give a fresh retry budget and ``available_at`` is set to
    now, so the requeue scan re-dispatches the job — no new dispatch path. The row
    lock is *blocking* (never SKIP LOCKED): a concurrent redrive waits rather than
    silently dropping rows from the batch. The status filter is the only guard a
    DEAD_LETTER row needs — it holds no live lease, so there is nothing to fence.
    Non-existent or non-DEAD_LETTER ids are ignored (not counted).
    """
    now = timezone.now()
    with transaction.atomic():
        locked = Job.objects.filter(pk__in=job_ids, status=Job.Status.DEAD_LETTER).lock()
        jobs = list(locked)
        if not jobs:
            return 0
        Job.objects.filter(pk__in=[job.pk for job in jobs]).update(
            status=Job.Status.PENDING,
            attempts=0,
            available_at=now,
            leased_until=None,
            lease_token=None,
            error="",
            updated_at=now,
        )
        for job in jobs:
            _log_job("job.redriven", job)
            _broadcast(job)
    return len(jobs)


def record_progress(job: Job, percent: int) -> bool:
    """Fenced progress write + broadcast; False means the caller lost its lease.

    A False return tells a worker it is a zombie — the reaper reassigned its job —
    so it should stop rather than keep computing progress nobody owns.
    """
    return _fenced_write(job, progress=percent)


def claim_due_for_requeue() -> list[Job]:
    """Claim PENDING jobs whose backoff has elapsed; the caller publishes them.

    Keyed on `available_at` (set only on a scheduled retry), so this never touches
    a brand-new job — those have `available_at IS NULL` and belong to the outbox.
    `available_at` is pushed forward by a visibility window before returning, so a
    job isn't re-claimed every tick while it waits to be processed; the claim
    clears it, and a lost message simply becomes due again after the window
    (self-healing). Publishing after this commit — not inside it — means a
    rollback can never leak a message for un-pushed state.
    """
    now = timezone.now()
    with transaction.atomic():
        due = (
            Job.objects.filter(
                status=Job.Status.PENDING,
                available_at__isnull=False,
                available_at__lte=now,
            )
            .order_by("available_at")
            .lock_for_claim()
        )
        jobs = list(due[:SCAN_BATCH_SIZE])
        if jobs:
            Job.objects.filter(pk__in=[job.pk for job in jobs]).update(
                available_at=now + timedelta(seconds=settings.JOB_REQUEUE_VISIBILITY_SECONDS),
                updated_at=now,
            )
    return jobs


def retry_delay(attempts: int) -> float:
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


def _terminal(
    job: Job,
    status: str,
    *,
    progress: int | None = None,
    result: dict | None = None,
    error: str = "",
) -> bool:
    """Move the job to a terminal state, releasing the lease (fenced write)."""
    fields: dict = {
        "status": status,
        "leased_until": None,
        "lease_token": None,
        "available_at": None,
        "error": error,
        # Terminal transition — stamp once for the processing-latency histogram.
        "finished_at": timezone.now(),
    }
    if progress is not None:
        fields["progress"] = progress
    if result is not None:
        fields["result"] = result
    return _fenced_write(job, **fields)


def _fenced_write(job: Job, **fields: Any) -> bool:
    """Write `fields` to the job only while it is still PROCESSING under our token.

    The status + lease_token guard fences a reclaimed-then-resumed slow worker: once
    the reaper has handed the job to someone else, this stale write matches zero rows
    and is discarded. A landed write broadcasts; a fenced-out one logs and stays
    silent — the live owner broadcasts its own writes.
    """
    fields["updated_at"] = timezone.now()
    landed = (
        Job.objects.filter(
            pk=job.id,
            status=Job.Status.PROCESSING,
            lease_token=job.lease_token,
        ).update(**fields)
        == 1
    )
    if landed:
        _broadcast(job)
    else:
        _log_job(
            "job.write_fenced",
            job,
            level=logging.WARNING,
            target_status=fields.get("status"),
        )
    return landed


def _broadcast(job: Job) -> None:
    """Schedule the transition's broadcast for after commit (immediate in autocommit)."""
    transaction.on_commit(lambda: notify_job(job))


def _processing_ms(job: Job) -> int:
    """Milliseconds since this attempt's claim (`started_at`), for the success log."""
    if job.started_at is None:
        return 0
    return round((timezone.now() - job.started_at).total_seconds() * 1000)


def _log_job(event: str, job: Job, *, level: int = logging.INFO, **fields: Any) -> None:
    """Structured transition event, auto-tagging job_id and attempts."""
    logger.log(level, event, extra={"job_id": job.id, "attempts": job.attempts, **fields})
