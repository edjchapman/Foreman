"""Sync -> async boundary for realtime job status.

`notify_job` is the *only* place the synchronous world (Celery tasks) hands a job
snapshot to the asynchronous channel layer. It re-reads the row so every broadcast
reflects committed state — the task's `_fenced_update`/`_terminal`/progress writes never
refresh the passed instance — serialises with the sync DRF serializer, and fans out to
the job's group. Broadcasting is **best-effort**: a channel-layer outage is logged and
swallowed so realtime can never fail a job. See ADR 0004.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from config.otel import get_tracer, inject_trace

from .models import Job
from .serializers import JobSerializer

logger = logging.getLogger(__name__)

# Fixed, non-parameterised group every queue-board client joins — the firehose of all job
# transitions, fanned out alongside each job's own group so the demo board can watch the
# whole queue live. See ADR 0004.
QUEUE_GROUP = "queue"


def job_group(job_id: str) -> str:
    """Channel-layer group name carrying one job's updates."""
    return f"job.{job_id}"


def notify_job(job: Job) -> None:
    """Broadcast the job's current serialized state to its WebSocket groups (best-effort).

    Serialises the committed row once and fans out twice: to the job's own group (the
    single-job stream) and to `QUEUE_GROUP` (the live queue board). Both sends share the
    best-effort guard — a channel-layer outage is logged once and swallowed so realtime can
    never fail a job.
    """
    layer = get_channel_layer()
    if layer is None:  # realtime not configured (e.g. a bare management command) — no-op
        return
    fresh = Job.objects.filter(pk=job.pk).first()  # re-fetch → committed, non-stale state
    if fresh is None:
        return
    data = dict(JobSerializer(fresh).data)  # serialise once, address twice
    try:
        with get_tracer().start_as_current_span("notify_job"):
            # Carry this span's context in the message so the consumer's ws.send links back
            # to the worker that produced the update — completing the trace to the client.
            carrier = inject_trace()
            send = async_to_sync(layer.group_send)
            send(job_group(str(fresh.pk)), {"type": "job.update", "data": data, "trace": carrier})
            send(QUEUE_GROUP, {"type": "queue.job", "data": data, "trace": carrier})
    except Exception:  # noqa: BLE001 — realtime is best-effort; never fail a job on broadcast
        logger.warning("realtime.notify_failed", extra={"job_id": fresh.pk})
