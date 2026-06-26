# ADR 0001 — Transactional outbox for job dispatch

- **Status:** Accepted
- **Milestone:** M2 (async worker + transactional outbox)

## Context

Submitting a job must do two things: persist the `Job` row and enqueue work for a
background worker. These are two different systems — PostgreSQL and the Celery
broker (Redis). Doing them as two independent writes is the classic **dual-write
problem**:

- Commit the job, then enqueue — if the process dies in between, the job is stored
  but never processed (lost work).
- Enqueue, then commit — if the commit rolls back, a worker picks up a job that
  doesn't exist (phantom work).

There is no distributed transaction across Postgres and Redis to lean on, and the
whole point of this project is reliability *beyond* CRUD, so "just call
`task.delay()` in the view" is not acceptable.

## Decision

Use a **transactional outbox**. Job submission writes the `Job` **and** an
`OutboxEvent` row in a single database transaction (`jobs.services.submit_job`).
A separate relay (`jobs.tasks.dispatch_outbox`, scheduled by Celery Beat) polls
PENDING outbox rows, publishes a `process_job` message for each, and marks them
DISPATCHED — all inside one transaction.

Because the job and its event commit atomically, we never enqueue work for a job
that didn't persist, and never persist a job whose event was lost. The event
carries a `payload` snapshot (`{"job_id": ...}`) so the relay is a dumb publisher
that never re-reads — and so never races — the job.

### Why these specifics

- **`SELECT ... FOR UPDATE SKIP LOCKED`** to claim rows: multiple relay instances
  grab disjoint batches without blocking each other. Guarded by a backend-feature
  check so the suite still runs on SQLite locally; the locking guarantee is a
  Postgres-runtime property exercised in CI.
- **Celery Beat relay** over a bespoke management-command loop: reuses the worker
  infrastructure we already stand up and is the idiomatic pattern.
- **`ForeignKey` to `Job`** over a bare aggregate UUID: outbox and job live in the
  same database and bounded context, so referential integrity and queryability win
  over the theoretical decoupling a loose id would buy.

## Consequences

- **Delivery is at-least-once.** A crash after `process_job.delay()` but before the
  relay commits leaves the row PENDING, so the job may be enqueued twice. This is
  the correct and intended outbox guarantee.
- **Exactly-once *effect* is the worker's job.** `process_job` flips
  PENDING → PROCESSING under a row lock and no-ops on any non-PENDING job, so a
  redelivered message does not reprocess. Full lease-based idempotency, retries
  with backoff, and a dead-letter path are **M3** — the schema is shaped so that
  work needs no migration churn (`attempts` exists; `PropertyRecord` leaves room
  for a natural-key uniqueness constraint to make re-import idempotent).
- **A polling relay adds latency** (the beat interval, ~1s) and load. Acceptable
  here; a `LISTEN/NOTIFY` push could replace polling later without changing the
  write path.
