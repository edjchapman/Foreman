"""Every job transition fans out via notify_job — including the on_commit seams.

The two atomic seams (claim, reaper) defer the broadcast to `transaction.on_commit`, so
they only fire on a real commit; `django_capture_on_commit_callbacks(execute=True)` runs
those callbacks deterministically. notify_job itself is stubbed to a recorder here — the
serialization is covered in test_realtime, the delivery in test_consumers.
"""

import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from jobs import tasks
from jobs.models import Job
from jobs.tasks import process_job
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db


def _raise_transient(job):
    raise RuntimeError("boom")


def _expired_lease_job(*, attempts):
    return JobFactory(
        status=Job.Status.PROCESSING,
        leased_until=timezone.now() - timedelta(seconds=1),
        lease_token=uuid.uuid4(),
        attempts=attempts,
    )


@pytest.fixture
def notified(monkeypatch):
    """Record the job ids passed to notify_job (the real broadcast is stubbed out)."""
    ids: list[str] = []
    monkeypatch.setattr("jobs.tasks.notify_job", lambda job: ids.append(str(job.pk)))
    return ids


def test_success_path_broadcasts(notified, django_capture_on_commit_callbacks):
    job = JobFactory()
    with django_capture_on_commit_callbacks(execute=True):  # fires the claim's on_commit
        assert process_job(str(job.id)) == "succeeded"
    assert str(job.id) in notified  # claim (on_commit) + progress + succeeded


def test_permanent_failure_broadcasts(notified, django_capture_on_commit_callbacks):
    job = JobFactory(payload={"source": "s3://bucket/data.csv"})
    with django_capture_on_commit_callbacks(execute=True):
        assert process_job(str(job.id)) == "failed"
    assert str(job.id) in notified


def test_transient_ladder_broadcasts(monkeypatch, notified, django_capture_on_commit_callbacks):
    monkeypatch.setattr(tasks, "_import_properties", _raise_transient)
    job = JobFactory()
    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(settings.JOB_MAX_ATTEMPTS):
            process_job(str(job.id))
    assert notified.count(str(job.id)) >= settings.JOB_MAX_ATTEMPTS  # retries + dead-letter


def test_reaper_dead_letter_broadcasts_on_commit(notified, django_capture_on_commit_callbacks):
    _expired_lease_job(attempts=settings.JOB_MAX_ATTEMPTS)
    with django_capture_on_commit_callbacks(execute=True):
        assert tasks._reap_expired_leases() == 1
    assert len(notified) == 1  # the dead-letter branch's on_commit lambda fired


def test_reaper_requeue_broadcasts_on_commit(notified, django_capture_on_commit_callbacks):
    _expired_lease_job(attempts=1)
    with django_capture_on_commit_callbacks(execute=True):
        assert tasks._reap_expired_leases() == 1
    assert len(notified) == 1  # the requeue branch's on_commit lambda fired
