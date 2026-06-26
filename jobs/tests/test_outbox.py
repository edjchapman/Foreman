"""Tests for the transactional-outbox write path (`jobs.services.submit_job`)."""

import pytest
from django.db import IntegrityError

from jobs.models import Job, OutboxEvent
from jobs.services import submit_job

pytestmark = pytest.mark.django_db


def test_submit_job_writes_job_and_outbox_atomically():
    job, created = submit_job(
        job_type="property_csv_import",
        payload={"source": "sample:properties.csv"},
        idempotency_key=None,
    )

    assert created is True
    assert Job.objects.count() == 1
    assert OutboxEvent.objects.count() == 1

    event = OutboxEvent.objects.get()
    assert event.job_id == job.id
    assert event.status == OutboxEvent.Status.PENDING
    assert event.event_type == "job.created"
    assert event.payload == {"job_id": str(job.id)}
    assert event.dispatched_at is None


def test_outbox_failure_rolls_back_the_job(monkeypatch):
    """If the outbox insert fails, the Job must not persist — no orphaned jobs."""

    def boom(*args, **kwargs):
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(OutboxEvent.objects, "create", boom)

    with pytest.raises(RuntimeError):
        submit_job(
            job_type="property_csv_import",
            payload={"source": "sample:properties.csv"},
            idempotency_key=None,
        )

    assert Job.objects.count() == 0
    assert OutboxEvent.objects.count() == 0


def test_submit_job_is_idempotent_on_key():
    first, created_first = submit_job(
        job_type="property_csv_import",
        payload={"source": "sample:properties.csv"},
        idempotency_key="abc-123",
    )
    second, created_second = submit_job(
        job_type="property_csv_import",
        payload={"source": "sample:properties.csv"},
        idempotency_key="abc-123",
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    # The duplicate submit writes nothing — one job, one event.
    assert Job.objects.count() == 1
    assert OutboxEvent.objects.count() == 1


def test_submit_job_reraises_integrity_error_without_key(monkeypatch):
    """A constraint failure unrelated to an idempotency race must not be swallowed."""

    def boom(*args, **kwargs):
        raise IntegrityError("some other constraint")

    monkeypatch.setattr(Job.objects, "create", boom)

    with pytest.raises(IntegrityError):
        submit_job(
            job_type="property_csv_import",
            payload={"source": "sample:properties.csv"},
            idempotency_key=None,
        )

    assert Job.objects.count() == 0
