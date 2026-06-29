"""Write-side application services for the jobs app.

Keeps the transactional-outbox invariant — Job and its OutboxEvent commit
together or not at all — in one place the API and tests can both call.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Job, OutboxEvent


def submit_job(*, job_type: str, payload: dict, idempotency_key: str | None) -> tuple[Job, bool]:
    """Create a Job and its outbox event atomically.

    Returns ``(job, created)``. When ``idempotency_key`` matches an existing job,
    returns that job with ``created=False`` and writes nothing.
    """
    if idempotency_key:
        existing = Job.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing, False

    try:
        with transaction.atomic():
            job = Job.objects.create(
                job_type=job_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            OutboxEvent.objects.create(
                job=job,
                event_type="job.created",
                payload={"job_id": str(job.id)},
            )
    except IntegrityError:
        # Lost the race to a concurrent first submit with the same key — the unique
        # constraint rejected us; return the row the winner committed.
        if idempotency_key:
            existing = Job.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing, False
        raise

    return job, True


def redrive_dead_letter(job_ids: Iterable[str | UUID]) -> int:
    """Reset DEAD_LETTER jobs to PENDING for another run; returns the count redriven.

    ``attempts`` resets to give a fresh retry budget and ``available_at`` is set to now,
    so the ``recover_jobs`` requeue scan re-dispatches the job — no new dispatch path.
    Non-existent or non-DEAD_LETTER ids are ignored (not counted).
    """
    now = timezone.now()
    return Job.objects.filter(pk__in=job_ids, status=Job.Status.DEAD_LETTER).update(
        status=Job.Status.PENDING,
        attempts=0,
        available_at=now,
        leased_until=None,
        lease_token=None,
        error="",
        updated_at=now,
    )
