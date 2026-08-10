"""Write-side application services for the jobs app.

`submit_job` is the single entry into the Job state machine: it keeps the
transactional-outbox invariant — Job and its OutboxEvent commit together or not
at all — in one place the API and tests can both call. Every transition after
submission lives in `jobs/lifecycle.py`.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from config.otel import inject_trace

from .models import Job, OutboxEvent
from .realtime import notify_job


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
            # Persist the caller's trace context into the outbox row. The API's request
            # span is active here (Django auto-instrumentation), so `inject_trace` captures
            # it; the relay re-hydrates it at dispatch, bridging the transactional-outbox
            # gap that in-process/broker propagation can't cross. Empty when tracing is off
            # or there's no active span (a direct call / management command). See ADR 0008.
            OutboxEvent.objects.create(
                job=job,
                event_type="job.created",
                payload={"job_id": str(job.id), "trace": inject_trace()},
            )
            # Creation is the first observable transition (nothing → PENDING): broadcast
            # on commit so the push-only queue board shows the job before it is claimed.
            transaction.on_commit(lambda: notify_job(job))
    except IntegrityError:
        # Lost the race to a concurrent first submit with the same key — the unique
        # constraint rejected us; return the row the winner committed.
        if idempotency_key:
            existing = Job.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing, False
        raise

    return job, True
