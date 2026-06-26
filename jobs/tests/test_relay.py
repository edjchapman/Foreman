"""Tests for the outbox relay (`jobs.tasks.dispatch_outbox`).

The worker is stubbed out here so these assertions are about claim/dispatch only;
the end-to-end relay→worker path is covered in test_pipeline.py.
"""

import pytest

from jobs import tasks
from jobs.models import OutboxEvent
from jobs.services import submit_job

pytestmark = pytest.mark.django_db


@pytest.fixture
def captured_dispatch(monkeypatch):
    """Replace process_job.delay with a recorder so the relay does no real work."""
    calls: list[str] = []
    monkeypatch.setattr(tasks.process_job, "delay", lambda job_id: calls.append(job_id))
    return calls


def _submit(n: int) -> list[str]:
    return [
        str(
            submit_job(
                job_type="property_csv_import",
                payload={"source": "sample:properties.csv"},
                idempotency_key=None,
            )[0].id
        )
        for _ in range(n)
    ]


def test_dispatch_claims_all_pending_once(captured_dispatch):
    job_ids = _submit(3)

    dispatched = tasks.dispatch_outbox()

    assert dispatched == 3
    assert sorted(captured_dispatch) == sorted(job_ids)
    events = OutboxEvent.objects.all()
    assert all(e.status == OutboxEvent.Status.DISPATCHED for e in events)
    assert all(e.dispatched_at is not None for e in events)


def test_second_dispatch_is_a_noop(captured_dispatch):
    _submit(2)
    assert tasks.dispatch_outbox() == 2

    captured_dispatch.clear()
    assert tasks.dispatch_outbox() == 0
    assert captured_dispatch == []


def test_already_dispatched_events_are_ignored(captured_dispatch):
    _submit(1)
    OutboxEvent.objects.update(status=OutboxEvent.Status.DISPATCHED)

    assert tasks.dispatch_outbox() == 0
    assert captured_dispatch == []
