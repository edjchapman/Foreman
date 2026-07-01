"""WebSocket consumer tests (headless, via Channels' WebsocketCommunicator).

Postgres-only: `database_sync_to_async` runs the ORM on a second thread/connection, which
an in-memory SQLite DB (one connection == one empty database) can't share — so these skip
on the local SQLite fast path and validate in CI. `django_db(transaction=True)` gives the
committed, cross-connection visibility the snapshot read needs. The channel layer is the
autouse InMemory one from conftest.
"""

import uuid

import pytest
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.db import connection

from jobs.realtime import job_group
from jobs.routing import websocket_urlpatterns
from jobs.tests.factories import JobFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="database_sync_to_async uses a second connection; in-memory SQLite can't share it",
    ),
]


def _app():
    return URLRouter(websocket_urlpatterns)


async def test_connect_sends_snapshot():
    job = await database_sync_to_async(JobFactory)()
    comm = WebsocketCommunicator(_app(), f"/ws/jobs/{job.id}/")

    connected, _ = await comm.connect()
    assert connected
    snapshot = await comm.receive_json_from()
    assert snapshot["id"] == str(job.id)
    assert snapshot["status"] == "PENDING"
    assert "attempts" in snapshot  # the field added for the live view

    await comm.disconnect()


async def test_unknown_job_closes_with_4404():
    comm = WebsocketCommunicator(_app(), f"/ws/jobs/{uuid.uuid4()}/")

    connected, code = await comm.connect()

    assert not connected
    assert code == 4404


async def test_group_update_is_forwarded_to_the_client():
    job = await database_sync_to_async(JobFactory)()
    comm = WebsocketCommunicator(_app(), f"/ws/jobs/{job.id}/")
    assert (await comm.connect())[0]
    await comm.receive_json_from()  # drain the snapshot

    await comm.send_json_to({"ignored": True})  # inbound is a no-op (covers receive_json)
    await get_channel_layer().group_send(
        job_group(str(job.id)),
        {"type": "job.update", "data": {"id": str(job.id), "status": "SUCCEEDED"}},
    )

    msg = await comm.receive_json_from()
    assert msg["status"] == "SUCCEEDED"
    await comm.disconnect()
