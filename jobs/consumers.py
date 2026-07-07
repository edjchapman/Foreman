"""WebSocket consumer streaming one job's live status.

A pure async passthrough: it re-fetches an authoritative snapshot on connect (via
`database_sync_to_async` — the one ORM hop) and then forwards `job.update` broadcasts
produced by `jobs.realtime.notify_job`. It never serialises or touches the ORM off the
sync boundary beyond that snapshot read, so it can't hit `SynchronousOnlyOperation`.
See ADR 0004.
"""

from __future__ import annotations

from typing import Any

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from config.otel import span_from_carrier

from .models import Job
from .realtime import QUEUE_GROUP, job_group
from .serializers import JobSerializer

JOB_NOT_FOUND_CLOSE_CODE = 4404
QUEUE_BOARD_SIZE = 20  # newest N jobs the board shows; client caps to the same


class JobStatusConsumer(AsyncJsonWebsocketConsumer):  # type: ignore[misc]  # channels base is untyped
    """Stream ``PENDING -> ... -> terminal`` for the job named in the URL."""

    async def connect(self) -> None:
        self.job_id = self.scope["url_route"]["kwargs"]["job_id"]
        snapshot = await database_sync_to_async(self._snapshot)()
        if snapshot is None:  # unknown job — reject before accepting
            await self.close(code=JOB_NOT_FOUND_CLOSE_CODE)
            return
        await self.accept()
        await self.send_json(snapshot)  # authoritative state first, then deltas
        self.group_name = job_group(str(self.job_id))
        await self.channel_layer.group_add(self.group_name, self.channel_name)

    async def disconnect(self, code: int) -> None:
        if getattr(self, "group_name", ""):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def job_update(self, event: dict[str, Any]) -> None:
        """Handler for ``type: job.update`` group messages — forward the payload."""
        with span_from_carrier("ws.send", event.get("trace")):
            await self.send_json(event["data"])

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        """Inbound is ignored — this is a server -> client stream."""

    def _snapshot(self) -> dict[str, Any] | None:
        job = Job.objects.filter(pk=self.job_id).first()
        return dict(JobSerializer(job).data) if job is not None else None


class QueueConsumer(AsyncJsonWebsocketConsumer):  # type: ignore[misc]  # channels base is untyped
    """Stream the whole queue: a snapshot of the newest jobs, then every live transition.

    Unlike `JobStatusConsumer` (which snapshots *before* joining), this joins `QUEUE_GROUP`
    *first* so no update is lost in the race window, then sends the snapshot. Any overlap is
    harmless because the client upserts by id — a job appearing in both the snapshot and an
    immediately-following live frame is idempotent. Channels buffers group messages until
    `connect` returns, so the client still sees the snapshot frame first.
    """

    async def connect(self) -> None:
        await self.accept()
        await self.channel_layer.group_add(QUEUE_GROUP, self.channel_name)
        jobs = await database_sync_to_async(self._recent)()
        await self.send_json({"type": "queue.snapshot", "jobs": jobs})

    async def disconnect(self, code: int) -> None:
        await self.channel_layer.group_discard(QUEUE_GROUP, self.channel_name)

    async def queue_job(self, event: dict[str, Any]) -> None:
        """Handler for ``type: queue.job`` group messages — forward one job's new state."""
        with span_from_carrier("ws.send", event.get("trace")):
            await self.send_json({"type": "queue.job", "job": event["data"]})

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        """Inbound is ignored — this is a server -> client stream."""

    def _recent(self) -> list[dict[str, Any]]:
        jobs = Job.objects.order_by("-created_at")[:QUEUE_BOARD_SIZE]
        return [dict(JobSerializer(job).data) for job in jobs]
