"""M3 reliability tests: idempotent re-import, retries, backoff, dead-letter,
lease-expiry crash recovery (the reaper), and operator redrive.
"""

import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from jobs import tasks
from jobs.models import Job, PropertyRecord
from jobs.tasks import process_job
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db


def _raise_transient(job):
    """Stand-in for an import that hits a transient (non-IngestError) failure."""
    raise RuntimeError("boom")


# --- Exactly-once effect (idempotent re-import) -----------------------------------


def test_reprocessing_a_job_does_not_duplicate_rows():
    """A redelivered job re-imports onto the same rows, not duplicates.

    The PENDING-guard already no-ops a redelivered *terminal* job, so to exercise the
    data-level guard directly we force the job back to PENDING and run it again. The
    unique (job, external_id) constraint + bulk_create(ignore_conflicts=True) keep the
    row count stable — exactly-once effect even if the status guard is ever bypassed.
    """
    job = JobFactory()  # sample:properties.csv → 5 rows

    assert process_job(str(job.id)) == "succeeded"
    assert PropertyRecord.objects.filter(job=job).count() == 5

    Job.objects.filter(pk=job.id).update(status=Job.Status.PENDING)
    assert process_job(str(job.id)) == "succeeded"

    assert PropertyRecord.objects.filter(job=job).count() == 5  # no duplicates


def test_distinct_jobs_may_share_external_ids():
    """Uniqueness is scoped per-job, so two imports of the same source never collide."""
    job_a = JobFactory()
    job_b = JobFactory()

    assert process_job(str(job_a.id)) == "succeeded"
    assert process_job(str(job_b.id)) == "succeeded"

    assert PropertyRecord.objects.filter(job=job_a).count() == 5
    assert PropertyRecord.objects.filter(job=job_b).count() == 5
    assert PropertyRecord.objects.count() == 10  # same external_ids, different jobs


def test_successful_job_releases_its_lease():
    job = JobFactory()

    assert process_job(str(job.id)) == "succeeded"

    job.refresh_from_db()
    assert job.leased_until is None
    assert job.lease_token is None
    assert job.available_at is None


# --- Retries, backoff, dead-letter ------------------------------------------------


def test_transient_failure_schedules_a_retry(monkeypatch):
    monkeypatch.setattr(tasks, "_import_properties", _raise_transient)
    job = JobFactory()

    outcome = process_job(str(job.id))

    job.refresh_from_db()
    assert outcome == "retry"
    assert job.status == Job.Status.PENDING
    assert job.attempts == 1
    assert job.available_at is not None  # scheduled for a future attempt
    assert job.leased_until is None  # lease released
    assert "boom" in job.error


def test_transient_failures_dead_letter_once_attempts_exhausted(monkeypatch):
    monkeypatch.setattr(tasks, "_import_properties", _raise_transient)
    job = JobFactory()

    # The claim ignores available_at, so calling process_job directly drives the ladder.
    outcomes = [process_job(str(job.id)) for _ in range(settings.JOB_MAX_ATTEMPTS)]

    job.refresh_from_db()
    assert outcomes[:-1] == ["retry"] * (settings.JOB_MAX_ATTEMPTS - 1)
    assert outcomes[-1] == "dead_letter"
    assert job.status == Job.Status.DEAD_LETTER
    assert job.attempts == settings.JOB_MAX_ATTEMPTS
    assert job.available_at is None  # terminal — no further retry scheduled


def test_permanent_failure_does_not_retry():
    """An IngestError is poison input: FAILED immediately, never retried."""
    job = JobFactory(payload={"source": "s3://bucket/data.csv"})

    outcome = process_job(str(job.id))

    job.refresh_from_db()
    assert outcome == "failed"
    assert job.status == Job.Status.FAILED
    assert job.attempts == 1  # one attempt, no retries
    assert job.available_at is None


# --- Retry scheduling: the recover_jobs requeue lane ------------------------------


def test_recover_jobs_requeues_only_due_retries(monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(tasks.process_job, "delay", lambda job_id: dispatched.append(job_id))

    due = JobFactory(status=Job.Status.PENDING, available_at=timezone.now() - timedelta(seconds=1))
    JobFactory(  # not yet due
        status=Job.Status.PENDING, available_at=timezone.now() + timedelta(hours=1)
    )
    JobFactory(status=Job.Status.PENDING)  # brand-new (available_at NULL) — outbox's job

    result = tasks.recover_jobs()

    assert result == {"reaped": 0, "requeued": 1}
    assert dispatched == [str(due.id)]
    due.refresh_from_db()
    assert due.available_at > timezone.now()  # bumped forward by the visibility window


# --- Backoff curve ----------------------------------------------------------------


@pytest.mark.parametrize(("attempts", "multiplier"), [(1, 1), (2, 2), (3, 4)])
def test_retry_delay_doubles_each_attempt(monkeypatch, attempts, multiplier):
    # Pin the jitter to its ceiling to assert the exponential schedule exactly.
    monkeypatch.setattr(tasks.random, "uniform", lambda lo, hi: hi)
    assert tasks._retry_delay(attempts) == settings.JOB_RETRY_BASE_SECONDS * multiplier


def test_retry_delay_is_capped(monkeypatch):
    # A high attempt count saturates at the ceiling, not base * 2**(n-1).
    monkeypatch.setattr(tasks.random, "uniform", lambda lo, hi: hi)
    assert tasks._retry_delay(99) == settings.JOB_RETRY_MAX_SECONDS


# A loop (not parametrize) for the Monte-Carlo samples: 20 draws per attempt assert one
# property, so a loop reads better than 100 near-identical parametrized cases.
@pytest.mark.parametrize("attempts", [1, 2, 3, 4, 5])
def test_retry_delay_stays_within_the_jitter_window(attempts):
    ceiling = min(
        settings.JOB_RETRY_MAX_SECONDS,
        settings.JOB_RETRY_BASE_SECONDS * 2 ** (attempts - 1),
    )
    for _ in range(20):
        assert 0 <= tasks._retry_delay(attempts) <= ceiling


# --- Lease-based crash recovery (the reaper) --------------------------------------


def _processing_with_lease(*, leased_until, attempts):
    return JobFactory(
        status=Job.Status.PROCESSING,
        leased_until=leased_until,
        lease_token=uuid.uuid4(),
        attempts=attempts,
    )


def test_reaper_requeues_a_job_whose_lease_expired():
    job = _processing_with_lease(leased_until=timezone.now() - timedelta(seconds=1), attempts=1)

    assert tasks._reap_expired_leases() == 1

    job.refresh_from_db()
    assert job.status == Job.Status.PENDING
    assert job.available_at is not None  # due now → requeue lane re-dispatches it
    assert job.leased_until is None
    assert job.attempts == 1  # reaper does not re-increment
    assert "lease expired" in job.error


def test_reaper_dead_letters_when_attempts_are_exhausted():
    job = _processing_with_lease(
        leased_until=timezone.now() - timedelta(seconds=1), attempts=settings.JOB_MAX_ATTEMPTS
    )

    assert tasks._reap_expired_leases() == 1

    job.refresh_from_db()
    assert job.status == Job.Status.DEAD_LETTER
    assert job.leased_until is None
    assert "lease expired" in job.error


def test_reaper_ignores_an_unexpired_lease():
    job = _processing_with_lease(leased_until=timezone.now() + timedelta(seconds=60), attempts=1)

    assert tasks._reap_expired_leases() == 0

    job.refresh_from_db()
    assert job.status == Job.Status.PROCESSING  # untouched


def test_a_stale_lease_token_write_is_fenced_out():
    """A reclaimed-then-resumed worker's terminal write must not clobber the row."""
    job = JobFactory()
    stale = tasks._claim_pending(str(job.id))  # worker A claims → token T1

    # Simulate a reaper reset + a fresh claim by worker B → new token T2.
    Job.objects.filter(pk=job.id).update(status=Job.Status.PENDING, lease_token=None)
    fresh = tasks._claim_pending(str(job.id))
    assert fresh.lease_token != stale.lease_token

    # Worker A finishes late and tries to mark SUCCEEDED with its stale token.
    tasks._terminal(stale, Job.Status.SUCCEEDED, progress=100)

    job.refresh_from_db()
    assert job.status == Job.Status.PROCESSING  # B still owns it; A's write was discarded
    assert job.lease_token == fresh.lease_token


# --- Operator redrive -------------------------------------------------------------


def test_redrive_returns_a_dead_letter_job_to_pending():
    job = JobFactory(status=Job.Status.DEAD_LETTER, attempts=3, error="boom")

    call_command("redrive", str(job.id))

    job.refresh_from_db()
    assert job.status == Job.Status.PENDING
    assert job.attempts == 0
    assert job.available_at is not None  # due now → requeue lane picks it up
    assert job.error == ""


def test_redrive_refuses_a_non_dead_letter_job():
    job = JobFactory(status=Job.Status.SUCCEEDED)

    with pytest.raises(CommandError):
        call_command("redrive", str(job.id))

    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED  # untouched


def test_redrive_skips_an_invalid_uuid():
    with pytest.raises(CommandError):
        call_command("redrive", "not-a-uuid")


def test_admin_redrive_action_only_touches_dead_letter_jobs(monkeypatch):
    from django.contrib.admin.sites import AdminSite

    from jobs.admin import JobAdmin

    dead = JobFactory(status=Job.Status.DEAD_LETTER, attempts=3)
    alive = JobFactory(status=Job.Status.SUCCEEDED)
    admin_instance = JobAdmin(Job, AdminSite())
    monkeypatch.setattr(admin_instance, "message_user", lambda *a, **k: None)

    admin_instance.redrive(request=None, queryset=Job.objects.all())

    dead.refresh_from_db()
    alive.refresh_from_db()
    assert dead.status == Job.Status.PENDING
    assert alive.status == Job.Status.SUCCEEDED  # service filters to DEAD_LETTER
