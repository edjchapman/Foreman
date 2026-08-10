"""Contract tests for `jobs.lifecycle`: every observable Job write broadcasts.

The module's invariant — commit first, then broadcast; a fenced-out write returns
False and stays silent — is proven here once, at the interface, instead of being
policed per call site (the old test_broadcast_seams.py). notify_job itself is
stubbed to a recorder: serialization is covered in test_realtime, delivery in
test_consumers. `django_capture_on_commit_callbacks(execute=True)` runs only the
callbacks registered inside its block, so setup transitions outside the block
never pollute the recorder.
"""

import uuid

import pytest

from jobs import lifecycle
from jobs.models import Job
from jobs.services import submit_job
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def notified(monkeypatch):
    """Record the job ids passed to notify_job (the real broadcast is stubbed out)."""
    ids: list[str] = []
    recorder = lambda job: ids.append(str(job.pk))  # noqa: E731 — two targets, one recorder
    monkeypatch.setattr("jobs.lifecycle.notify_job", recorder)
    monkeypatch.setattr("jobs.services.notify_job", recorder)
    return ids


def _stale_and_fresh_claims(job):
    """Claim twice around a simulated reap, returning (stale, fresh) lease holders."""
    stale = lifecycle.claim(str(job.id))
    Job.objects.filter(pk=job.id).update(status=Job.Status.PENDING, lease_token=None)
    fresh = lifecycle.claim(str(job.id))
    return stale, fresh


def test_claim_broadcasts_only_on_commit(notified, django_capture_on_commit_callbacks):
    job = JobFactory()
    with django_capture_on_commit_callbacks(execute=True):
        assert lifecycle.claim(str(job.id)) is not None
        assert notified == []  # deferred: nothing fires inside the transaction
    assert notified == [str(job.id)]  # fires exactly once, on commit


def test_fenced_terminal_write_stays_silent(notified, django_capture_on_commit_callbacks):
    job = JobFactory()
    stale, fresh = _stale_and_fresh_claims(job)
    with django_capture_on_commit_callbacks(execute=True):
        assert lifecycle.succeed(stale, result={}) is False
    assert notified == []  # the live owner broadcasts its own writes, not the zombie
    job.refresh_from_db()
    assert job.lease_token == fresh.lease_token  # fresh lease untouched


def test_record_progress_broadcasts_and_persists(notified, django_capture_on_commit_callbacks):
    job = JobFactory()
    owner = lifecycle.claim(str(job.id))
    with django_capture_on_commit_callbacks(execute=True):
        assert lifecycle.record_progress(owner, 40) is True
    assert notified == [str(job.id)]
    job.refresh_from_db()
    assert job.progress == 40


def test_fenced_progress_write_stays_silent(notified, django_capture_on_commit_callbacks):
    job = JobFactory()
    stale, _ = _stale_and_fresh_claims(job)
    with django_capture_on_commit_callbacks(execute=True):
        assert lifecycle.record_progress(stale, 40) is False
    assert notified == []


def test_redrive_broadcasts_each_redriven_job(notified, django_capture_on_commit_callbacks):
    dead_a = JobFactory(status=Job.Status.DEAD_LETTER, attempts=3, error="boom")
    dead_b = JobFactory(status=Job.Status.DEAD_LETTER, attempts=3, error="boom")
    alive = JobFactory(status=Job.Status.SUCCEEDED)

    with django_capture_on_commit_callbacks(execute=True):
        count = lifecycle.redrive([dead_a.id, dead_b.id, alive.id, uuid.uuid4()])

    assert count == 2  # non-DEAD_LETTER and unknown ids are ignored, not counted
    assert sorted(notified) == sorted([str(dead_a.id), str(dead_b.id)])
    dead_a.refresh_from_db()
    assert dead_a.status == Job.Status.PENDING
    assert dead_a.attempts == 0  # fresh retry budget
    assert dead_a.available_at is not None  # due now → requeue lane picks it up
    assert dead_a.error == ""
    alive.refresh_from_db()
    assert alive.status == Job.Status.SUCCEEDED  # untouched


def test_submit_job_broadcasts_creation(notified, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        job, created = submit_job(
            job_type="property_csv_import",
            payload={"source": "sample:properties.csv"},
            idempotency_key=None,
        )
    assert created is True
    assert notified == [str(job.id)]  # the push-only queue board sees PENDING arrivals


def test_idempotent_resubmit_does_not_broadcast(notified, django_capture_on_commit_callbacks):
    existing = JobFactory(idempotency_key="key-1")
    with django_capture_on_commit_callbacks(execute=True):
        job, created = submit_job(
            job_type="property_csv_import",
            payload={"source": "sample:properties.csv"},
            idempotency_key="key-1",
        )
    assert created is False
    assert job.id == existing.id
    assert notified == []  # nothing was written, so nothing is announced
