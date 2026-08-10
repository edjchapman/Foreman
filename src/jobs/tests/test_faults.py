"""Demo fault-injection sources drive the real retry / dead-letter / heal paths.

These are the synthetic sources the demo page uses to make the reliability
machinery visible; the tests assert they flow through the *transient* branch
(retry, then dead-letter) — never the poison `FAILED` branch — and heal into a
real import once the fault clears. See jobs/faults.py.
"""

import pytest
from django.conf import settings
from django.utils import timezone

from jobs import faults
from jobs.faults import MAX_SLEEP_SECONDS, InjectedFaultError, is_fault_source, load_fault_csv
from jobs.models import Job, PropertyRecord
from jobs.tasks import process_job
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db

NOW = timezone.now()


# --- Unit: the source resolver ----------------------------------------------------


def test_is_fault_source_detects_the_prefix():
    assert is_fault_source("fault:flaky")
    assert is_fault_source("fault:heal-after:20")
    assert not is_fault_source("sample:properties.csv")
    assert not is_fault_source("s3://bucket/x.csv")


def test_flaky_raises_a_transient_error_until_the_last_attempt():
    # A non-IngestError → the worker's transient (retry) branch, not poison FAILED.
    with pytest.raises(InjectedFaultError):
        load_fault_csv("fault:flaky", attempts=1, created_at=NOW)
    # On the final budgeted attempt it heals into the real sample CSV.
    csv = load_fault_csv("fault:flaky", attempts=settings.JOB_MAX_ATTEMPTS, created_at=NOW)
    assert "external_id" in csv  # the bundled sample header


def test_heal_after_raises_inside_the_window_and_heals_past_it():
    with pytest.raises(InjectedFaultError):
        load_fault_csv("fault:heal-after:3600", attempts=1, created_at=NOW)
    csv = load_fault_csv("fault:heal-after:0", attempts=1, created_at=NOW)
    assert "external_id" in csv


def test_unknown_fault_source_is_transient_not_permanent():
    with pytest.raises(InjectedFaultError):
        load_fault_csv("fault:nonsense", attempts=1, created_at=NOW)


def test_malformed_heal_after_window_is_transient():
    with pytest.raises(InjectedFaultError):
        load_fault_csv("fault:heal-after:soon", attempts=1, created_at=NOW)


def test_sleep_source_naps_then_heals(monkeypatch):
    naps: list[float] = []
    monkeypatch.setattr(faults.time, "sleep", naps.append)

    csv = load_fault_csv("fault:sleep:0.5", attempts=1, created_at=NOW)

    assert naps == [0.5]
    assert "external_id" in csv  # heals into the real sample CSV


def test_sleep_duration_is_capped(monkeypatch):
    # The API is open, so a huge duration must not hold a worker slot indefinitely.
    naps: list[float] = []
    monkeypatch.setattr(faults.time, "sleep", naps.append)

    load_fault_csv("fault:sleep:99999", attempts=1, created_at=NOW)

    assert naps == [MAX_SLEEP_SECONDS]


def test_malformed_sleep_duration_is_transient():
    with pytest.raises(InjectedFaultError):
        load_fault_csv("fault:sleep:forever", attempts=1, created_at=NOW)


# --- Integration: process_job through the fault sources ----------------------------


def test_flaky_job_retries_then_recovers():
    """attempts climb, each early attempt reschedules, and the last one succeeds."""
    job = JobFactory(payload={"source": "fault:flaky"})

    outcomes = [process_job(str(job.id)) for _ in range(settings.JOB_MAX_ATTEMPTS)]

    job.refresh_from_db()
    assert outcomes[:-1] == ["retry"] * (settings.JOB_MAX_ATTEMPTS - 1)
    assert outcomes[-1] == "succeeded"
    assert job.status == Job.Status.SUCCEEDED
    assert PropertyRecord.objects.filter(job=job).count() == 5  # healed into a real import


def test_heal_after_job_dead_letters_while_the_window_holds():
    job = JobFactory(payload={"source": "fault:heal-after:3600"})

    outcomes = [process_job(str(job.id)) for _ in range(settings.JOB_MAX_ATTEMPTS)]

    job.refresh_from_db()
    assert outcomes[-1] == "dead_letter"
    assert job.status == Job.Status.DEAD_LETTER
    assert job.attempts == settings.JOB_MAX_ATTEMPTS


def test_heal_after_job_succeeds_once_the_window_has_passed():
    """The same source imports cleanly once the outage 'heals' — the redrive payoff."""
    job = JobFactory(payload={"source": "fault:heal-after:0"})

    assert process_job(str(job.id)) == "succeeded"

    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED
    assert PropertyRecord.objects.filter(job=job).count() == 5


def test_sleep_job_imports_after_the_nap():
    job = JobFactory(payload={"source": "fault:sleep:0"})

    assert process_job(str(job.id)) == "succeeded"

    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED
    assert PropertyRecord.objects.filter(job=job).count() == 5
