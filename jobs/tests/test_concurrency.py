"""Real-concurrency tests for the SKIP LOCKED claim paths (Postgres only).

The rest of the suite is single-threaded, so it can only verify that `_lock_for_claim`
*emits* SELECT ... FOR UPDATE SKIP LOCKED — not that concurrent claimers actually get
disjoint rows. These tests exercise the property itself: N threads, each on its own DB
connection, race the claim functions against a shared Postgres and assert exactly-one
claim, disjoint outbox batches, no double-processing, and no torn rows in the
reaper-vs-claim race.

Mechanics: `transaction=True` commits writes for real (cross-connection visibility;
pytest-django truncates afterwards). Threads never assert — each records its result or
exception and the main thread asserts after joining, so a thread failure surfaces as a
test failure instead of a silently dead thread. Every thread closes its own connection
in `finally`: teardown's TRUNCATE waits on any connection still holding locks.
"""

import threading
import time
import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from jobs import tasks
from jobs.models import Job, OutboxEvent, PropertyRecord
from jobs.tasks import OUTBOX_BATCH_SIZE, process_job
from jobs.tests.factories import JobFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="SKIP LOCKED claim disjointness is a Postgres runtime property",
    ),
]


def _run_threads(count: int, fn):
    """Run `fn(index)` on `count` threads released together by a barrier.

    Returns the per-thread results; re-raises the first exception any thread hit.
    """
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def runner(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = fn(index)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the main thread below
            errors.append(exc)
        finally:
            connection.close()  # thread-local connection; see module docstring

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "worker thread hung"
    if errors:
        raise errors[0]
    return results


def test_only_one_of_many_concurrent_claims_wins():
    """Eight simultaneous `_claim_pending` calls on one job yield exactly one winner."""
    job = JobFactory()

    claims = _run_threads(8, lambda i: tasks._claim_pending(str(job.id)))

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    job.refresh_from_db()
    assert job.status == Job.Status.PROCESSING
    assert job.attempts == 1  # exactly one claim incremented
    assert job.lease_token == winners[0].lease_token


def test_parallel_relays_dispatch_disjoint_outbox_batches(monkeypatch):
    """Four relay loops over one outbox publish every event exactly once."""
    total = OUTBOX_BATCH_SIZE * 2 + 50  # spans multiple claim batches per relay
    jobs = Job.objects.bulk_create(Job() for _ in range(total))
    OutboxEvent.objects.bulk_create(
        OutboxEvent(job=job, payload={"job_id": str(job.id)}) for job in jobs
    )

    published: list[str] = []
    publish_lock = threading.Lock()

    def record(job_id: str) -> None:
        with publish_lock:
            published.append(job_id)

    monkeypatch.setattr(tasks.process_job, "delay", record)

    def relay(_: int) -> int:
        dispatched = 0
        while batch := tasks.dispatch_outbox():
            dispatched += batch
        return dispatched

    per_relay = _run_threads(4, relay)

    # Same sorted lists ⇔ every event published exactly once (complete AND duplicate-free).
    assert sorted(published) == sorted(str(job.id) for job in jobs)
    assert sum(per_relay) == total
    assert not OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).exists()


def test_concurrent_process_job_runs_import_exactly_once(monkeypatch):
    """Racing full worker runs: one imports, the rest no-op on the PROCESSING guard."""
    real_import = tasks._import_properties

    def slow_import(job: Job) -> dict:
        # Hold the winner inside its import window so the losers arrive while the
        # job is PROCESSING (mid-flight), not after it is already terminal.
        time.sleep(0.2)
        return real_import(job)

    monkeypatch.setattr(tasks, "_import_properties", slow_import)
    job = JobFactory()  # sample:properties.csv → 5 rows

    outcomes = _run_threads(4, lambda i: process_job(str(job.id)))

    assert sorted(outcomes) == ["skipped", "skipped", "skipped", "succeeded"]
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED
    assert job.attempts == 1
    assert PropertyRecord.objects.filter(job=job).count() == 5


def test_reaper_vs_claim_race_never_tears_the_row():
    """The reaper and a claimer racing an expired lease leave one consistent owner.

    Three interleavings are legal — reaper wins (job back to PENDING), reaper wins and
    the claimer then re-claims (fresh PROCESSING lease), or the claimer's brief row lock
    makes SKIP LOCKED skip the row entirely (untouched until the next reaper tick). What
    must never happen is a torn row: PENDING with a lease, or PROCESSING re-claimed
    without a fresh token. Several rounds shake out different interleavings.
    """
    for _ in range(5):
        original_token = uuid.uuid4()
        job = JobFactory(
            status=Job.Status.PROCESSING,
            leased_until=timezone.now() - timedelta(seconds=1),
            lease_token=original_token,
            attempts=1,
        )

        def contend(index: int, job_id: str = str(job.id)):
            if index == 0:
                return tasks._reap_expired_leases()
            return tasks._claim_pending(job_id)

        _run_threads(2, contend)

        job.refresh_from_db()
        if job.status == Job.Status.PENDING:
            # Reaped, not yet re-claimed: lease fully released, requeue lane armed.
            assert job.lease_token is None
            assert job.leased_until is None
            assert job.available_at is not None
            assert job.attempts == 1
        else:
            assert job.status == Job.Status.PROCESSING
            assert job.lease_token is not None
            assert job.attempts in (1, 2)
            if job.attempts == 2:  # reaped then re-claimed: must hold a fresh lease
                assert job.lease_token != original_token
                assert job.leased_until is not None
                assert job.leased_until > timezone.now()
            else:  # claimer's lock made the reaper skip: row untouched
                assert job.lease_token == original_token
