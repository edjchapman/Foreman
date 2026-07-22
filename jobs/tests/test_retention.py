"""Retention pruning: env-gated deletion of aged terminal jobs + outbox history.

Verifies the contract in jobs/retention.py: disabled by default; terminal jobs
past the horizon are pruned with their cascades; recent/non-terminal jobs and
PENDING outbox rows survive; batching terminates; and Beat schedules the task.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from jobs.models import Job, OutboxEvent, PropertyRecord
from jobs.retention import prune_expired
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db


def _aged_terminal_job(*, days_old: int, status: str = Job.Status.SUCCEEDED) -> Job:
    job = JobFactory(status=status)
    Job.objects.filter(pk=job.id).update(finished_at=timezone.now() - timedelta(days=days_old))
    return job


def test_disabled_by_default_keeps_everything():
    _aged_terminal_job(days_old=365)

    assert prune_expired() == {"jobs": 0, "outbox_events": 0}
    assert Job.objects.count() == 1


def test_prunes_aged_terminal_jobs_with_their_cascades(settings):
    settings.RETENTION_DAYS = 7
    job = _aged_terminal_job(days_old=8)
    PropertyRecord.objects.create(
        job=job, external_id="p1", address_line1="1 Test St", city="Leeds", postcode="LS1 1AA"
    )
    OutboxEvent.objects.create(job=job, payload={"job_id": str(job.id)})

    result = prune_expired()

    assert result["jobs"] == 1
    assert not Job.objects.exists()
    assert not PropertyRecord.objects.exists()  # CASCADE rode along
    assert not OutboxEvent.objects.exists()


@pytest.mark.parametrize(
    "status", [Job.Status.SUCCEEDED, Job.Status.FAILED, Job.Status.DEAD_LETTER]
)
def test_every_terminal_status_is_prunable(settings, status):
    settings.RETENTION_DAYS = 7
    _aged_terminal_job(days_old=8, status=status)

    assert prune_expired()["jobs"] == 1
    assert not Job.objects.exists()


def test_keeps_recent_terminal_and_aged_non_terminal_jobs(settings):
    settings.RETENTION_DAYS = 7
    recent = _aged_terminal_job(days_old=3)
    # Aged but non-terminal: no finished_at, so the horizon filter never matches it.
    old_pending = JobFactory(status=Job.Status.PENDING)
    Job.objects.filter(pk=old_pending.id).update(created_at=timezone.now() - timedelta(days=30))

    assert prune_expired() == {"jobs": 0, "outbox_events": 0}
    assert set(Job.objects.values_list("pk", flat=True)) == {recent.id, old_pending.id}


def test_prunes_aged_dispatched_outbox_but_never_pending(settings):
    settings.RETENTION_DAYS = 7
    job = JobFactory(status=Job.Status.PENDING)  # job itself survives
    old = timezone.now() - timedelta(days=8)
    dispatched = OutboxEvent.objects.create(
        job=job, payload={}, status=OutboxEvent.Status.DISPATCHED, dispatched_at=old
    )
    pending = OutboxEvent.objects.create(job=job, payload={})
    OutboxEvent.objects.filter(pk=pending.id).update(created_at=old)  # old but undispatched

    result = prune_expired()

    assert result["outbox_events"] == 1
    remaining = set(OutboxEvent.objects.values_list("pk", flat=True))
    assert dispatched.id not in remaining
    assert pending.id in remaining  # undispatched events are work, not history


def test_batching_prunes_past_a_single_batch(settings):
    settings.RETENTION_DAYS = 7
    settings.RETENTION_BATCH_SIZE = 2
    for _ in range(5):
        _aged_terminal_job(days_old=8)

    assert prune_expired()["jobs"] == 5
    assert not Job.objects.exists()


def test_beat_schedules_the_prune_task():
    entry = settings.CELERY_BEAT_SCHEDULE["prune-retention"]
    assert entry["task"] == "jobs.prune_expired"
