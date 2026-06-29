# ADR 0002 — Retries, dead-letter, and lease-based crash recovery

- **Status:** Accepted
- **Milestone:** M3 (reliability)
- **Extends:** [ADR 0001](0001-transactional-outbox.md), which delivered at-least-once
  delivery and named lease-based idempotency, retries, and a dead-letter path as M3.

## Context

M2 gives **at-least-once delivery** (the transactional outbox) plus a PENDING-guard:
`process_job` claims a job `PENDING → PROCESSING` under a row lock and no-ops on any
non-PENDING job, so a redelivered message does not reprocess. That left three gaps the
M3 reliability story has to close:

1. **Re-import was not idempotent** — a reprocessed job duplicated `PropertyRecord`s.
2. **Failures were terminal and undifferentiated** — a blanket `except Exception → FAILED`
   meant a transient blip (DB hiccup) was as fatal as poison input, and `DEAD_LETTER`
   was never reached.
3. **A worker that crashed *after* claiming stranded its job forever** — the job sat in
   `PROCESSING` with nothing to reclaim it, because the PENDING-guard (correctly) blocks
   the broker from redelivering an in-flight job.

## Decision

### Retry state lives in Postgres, not the broker

Retries are driven by the database (`Job.attempts` / `available_at` / `leased_until` /
`lease_token`) and Beat scans that mirror the outbox relay, **not** Celery's native
`self.retry()`. Native retry is not just off-theme here, it is *broken against this
design*: it redelivers the same message while the job is still `PROCESSING`, so the
PENDING-guard returns `"skipped"` and the job is stranded. Keeping retry state in
Postgres also makes it queryable (and ready for M4 to stream over WebSockets) and lets it
survive a broker restart.

### Failure taxonomy

`process_job` distinguishes two failure classes:

- **Permanent** — the ingest `IngestError` family (unknown/missing/unsupported source):
  poison input that can never succeed → `FAILED` immediately, never retried.
- **Transient** — any other exception → retry with backoff while
  `attempts < JOB_MAX_ATTEMPTS`, else `DEAD_LETTER`.

`attempts` is incremented once per *claim* (since M2), and a scheduled retry returns the
job to `PENDING` to be re-claimed, so the counter tallies total attempts across the whole
ladder with no separate field. The dead-letter check is simply
`attempts >= JOB_MAX_ATTEMPTS` against the post-increment value.

### Backoff

Exponential with **full jitter**, capped:
`delay = uniform(0, min(JOB_RETRY_MAX_SECONDS, JOB_RETRY_BASE_SECONDS · 2^(attempts-1)))`.
Full jitter spreads a herd of simultaneous failures across the window instead of retrying
them in lockstep.

### Two dispatch lanes, provably disjoint

- The **outbox relay** dispatches brand-new jobs once (`available_at IS NULL`; its event
  flips to `DISPATCHED` and is never recreated).
- **`recover_jobs`** (Beat) dispatches scheduled retries (`available_at` set and due).

A new job is invisible to the retry lane (its `available_at` is NULL) and a retry job is
invisible to the outbox (its event is already `DISPATCHED`) — the lanes partition on
`available_at` NULL-ness, so no job is ever dispatched by both or dropped by both. We
chose this over re-emitting a retry `OutboxEvent` (one dispatcher) because that would grow
the outbox table per retry, make the relay schedule-aware, and muddy the M2 invariant that
an `OutboxEvent` is one `job.created` domain event — and it still could not express
"the worker died mid-process," which needs the reaper below.

### Lease + reaper for crash recovery

`_claim_pending` records a lease (`leased_until = now + JOB_LEASE_SECONDS`). `recover_jobs`
reaps `PROCESSING` jobs whose lease has expired — their worker died mid-process — back to
`PENDING` (or `DEAD_LETTER` if attempts are spent). This is complementary to broker-level
recovery, which covers a *different* window:

| Crash window | Job state | Recovered by |
|---|---|---|
| Before the claim commits | still `PENDING` | broker redelivery (`acks_late` + `reject_on_worker_lost`) |
| After the claim commits | `PROCESSING` | **the lease reaper** (broker redelivery is blocked by the PENDING-guard) |

`worker_prefetch_multiplier = 1` keeps at most one unacked task per worker, so a single
SIGKILL strands at most one lease.

### Fencing token

Each claim stamps a fresh `lease_token`; every terminal/retry write is guarded on
`(status=PROCESSING, lease_token=<ours>)`. If a *slow* (not dead) worker is reaped and the
job re-claimed by another, the original worker's late write carries a stale token, matches
zero rows, and is discarded instead of clobbering the row. The per-job
`UniqueConstraint(job, external_id)` independently neutralises any duplicate rows such a
double-run would insert (`bulk_create(ignore_conflicts=True)`).

### Operator redrive

`python manage.py redrive <job_id>` (and a Django-admin action) resets a `DEAD_LETTER`
job to `PENDING` with fresh `attempts` and `available_at = now`; the requeue lane picks it
up — no new dispatch path.

## Consequences — documented failure modes

This is the M3 "documented failure modes" deliverable and the seed of the M4 runbook.

- **Residual stuck-`PENDING` window.** A permanently-lost message for a job that is
  `PENDING` with `available_at IS NULL` (a brand-new job whose outbox dispatch was lost
  after the event was marked `DISPATCHED`) is recovered only by broker
  `acks_late`/`visibility_timeout`. This is the irreducible boundary of any at-least-once
  system; we **document** it rather than add a third "stuck-pending" scanner.
- **Lease-reclaim race.** Between a lease expiring and the slow worker noticing, the reaper
  may re-dispatch and a second worker may run concurrently. Data damage is neutralised by
  the unique constraint; state damage is neutralised by the fencing token; frequency is
  minimised by setting `JOB_LEASE_SECONDS` comfortably above the worst-case import time.
- **Latency.** A due retry waits up to `RECOVER_POLL_SECONDS` to be re-dispatched; a
  crashed job waits up to `JOB_LEASE_SECONDS + RECOVER_POLL_SECONDS` to be reaped. Both are
  acceptable for this workload and tunable.
- **`result["rows_imported"]` reports target state** (rows that exist for the job after an
  idempotent re-run), not the count newly inserted — the correct semantic for at-least-once.

### Tunables (env-overridable; defaults)

`JOB_MAX_ATTEMPTS=3` · `JOB_RETRY_BASE_SECONDS=2` · `JOB_RETRY_MAX_SECONDS=300` ·
`JOB_LEASE_SECONDS=120` · `JOB_REQUEUE_VISIBILITY_SECONDS=60` · `RECOVER_POLL_SECONDS=5` ·
`CELERY_TASK_ACKS_LATE=true` · `CELERY_TASK_REJECT_ON_WORKER_LOST=true` ·
`CELERY_WORKER_PREFETCH_MULTIPLIER=1` · `CELERY_VISIBILITY_TIMEOUT=3600`.
