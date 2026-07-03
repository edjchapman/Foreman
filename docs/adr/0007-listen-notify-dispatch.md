# ADR 0007 — LISTEN/NOTIFY push-dispatch for the outbox

- **Status:** Accepted
- **Milestone:** M6 (prove it under load — incremental, post-M5)
- **Extends:** [ADR 0001](0001-transactional-outbox.md) (the outbox this optimizes)
  and [ADR 0006](0006-load-testing-metrics.md) (whose load test motivated it).

## Context

The [ADR 0006](0006-load-testing-metrics.md) load test measured the pipeline and
found a clear shape: **processing is fast (p95 ≈ 77 ms) but end-to-end latency is
dominated by queue wait (p95 ≈ 2.1 s)**. The cause is structural, not saturation:
the outbox relay only runs on the Celery **Beat poll** (`OUTBOX_POLL_SECONDS`,
default 1 s), so a freshly submitted job waits — on average half a poll interval,
and under a bursty backlog considerably more — before it is even dispatched to a
worker. Lowering the poll interval only trades latency for idle database load; the
latency floor is the polling model itself.

Postgres already offers the primitive to remove the poll from the common path:
**`LISTEN`/`NOTIFY`**, a push signal that is *delivered on transaction commit*.

## Decision

Add **push-dispatch**: notify on every outbox write, and run a listener that
dispatches immediately — while keeping Beat as the fallback.

### A DB trigger emits the notification

Migration 0006 installs an `AFTER INSERT ... FOR EACH STATEMENT` trigger on
`jobs_outboxevent` that calls `pg_notify('foreman_outbox', '')`. A **trigger**,
not an app-level `transaction.on_commit`, because:

- **It inherits NOTIFY's transactional delivery for free.** The notification is
  delivered exactly when the `Job` + `OutboxEvent` transaction commits — never for
  an uncommitted or rolled-back write — with no code at the call site.
- **It can't be forgotten.** Any current or future writer of an outbox row wakes
  the listener; there is no `notify()` call to remember to add.

It is **guarded to PostgreSQL** (the migration runs the SQL only when
`connection.vendor == "postgresql"`), matching how the codebase already fences
`SKIP LOCKED`; SQLite test runs simply skip the trigger.

### A dedicated listener process dispatches on the signal

`manage.py outbox_listener` opens one dedicated connection, `LISTEN`s on the
channel, and calls the existing `dispatch_outbox()` on each notification — the
same claim-and-publish path the Beat relay uses, now triggered in **milliseconds**
instead of on the next poll. It also sweeps on a slow timer, so it is self-healing
even if Beat is off, and shuts down cleanly on `SIGTERM`.

### Beat stays as the durability backstop

This is the crux: **NOTIFY is a wakeup, not a delivery guarantee.** A notification
raised while no listener is connected (a listener restart, a deploy) is simply
lost — Postgres does not queue it. The **outbox row is still the durable source of
truth**, and the Beat poll still dispatches it. So push-dispatch is a *latency
optimization layered on top of* the at-least-once machinery, never a replacement
for it: with the listener down, the system degrades to exactly today's behaviour.

## Consequences

- **Queue wait collapses.** In a paired load test, submit → first-claim dropped
  from **p50 733 ms / p95 1.84 s** (Beat poll) to **p50 41 ms / p95 0.34 s**
  (push-dispatch) — roughly **18× at the median, 5.5× at p95** — halving
  end-to-end latency, with throughput and the zero-failure result unchanged. The
  full before/after is in [docs/load-testing.md](../load-testing.md).
- **Correctness is unchanged.** The outbox, the two disjoint dispatch lanes, and
  at-least-once delivery are all untouched; the listener only makes the existing
  new-job lane fire sooner. Removing the listener is a safe, behaviour-preserving
  rollback.
- **One new (optional) service + one long-lived connection.** The `listener`
  Railway service and a single `LISTEN` connection. CD deploys it only once
  `RAILWAY_LISTENER_SERVICE_ID` is set, so the pipeline is unaffected until it is
  provisioned (see [docs/deploy.md](../deploy.md)).
- **The Beat poll can now be relaxed.** With push-dispatch carrying the common
  path, `OUTBOX_POLL_SECONDS` could be raised to cut idle DB load, trading only
  *fallback* latency. Left at 1 s by default — conservative until the listener has
  a track record.
- **Retries are deliberately not pushed.** The `recover_jobs` requeue lane keeps
  its 5 s poll: a retry is already intentionally delayed by its backoff, so push
  dispatch there would buy nothing.
- **Future work:** `LISTEN/NOTIFY` could also drive realtime fan-out or a
  work-stealing signal; for now it is scoped to the one measured bottleneck.
