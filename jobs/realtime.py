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

from .models import Job
from .serializers import JobSerializer

logger = logging.getLogger(__name__)


def job_group(job_id: str) -> str:
    """Channel-layer group name carrying one job's updates."""
    return f"job.{job_id}"


def notify_job(job: Job) -> None:
    """Broadcast the job's current serialized state to its WebSocket group (best-effort)."""
    layer = get_channel_layer()
    if layer is None:  # realtime not configured (e.g. a bare management command) — no-op
        return
    fresh = Job.objects.filter(pk=job.pk).first()  # re-fetch → committed, non-stale state
    if fresh is None:
        return
    message = {"type": "job.update", "data": dict(JobSerializer(fresh).data)}
    try:
        async_to_sync(layer.group_send)(job_group(str(fresh.pk)), message)
    except Exception:  # noqa: BLE001 — realtime is best-effort; never fail a job on broadcast
        logger.warning("realtime.notify_failed", extra={"job_id": fresh.pk})
