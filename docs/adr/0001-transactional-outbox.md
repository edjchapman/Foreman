# Transactional outbox for job dispatch

Submitting a job must persist the `Job` row *and* enqueue worker work — two
systems (Postgres, the Celery broker) with no shared transaction, i.e. the
dual-write problem: either order can lose work or emit phantom work on a crash.
So the API writes the `Job` and an `OutboxEvent` in one database transaction,
and a relay claims PENDING events (`SELECT … FOR UPDATE SKIP LOCKED`),
publishes each, and marks it DISPATCHED. The event snapshots its payload so the
relay never re-reads — and never races — the job.

## Consequences

- Delivery is **at-least-once** (a crash between publish and the relay's commit
  re-sends); exactly-once *effect* is the worker's job ([ADR 0002](0002-retries-dlq-lease.md)).
- The relay polls on Celery Beat (~1 s latency); [ADR 0007](0007-listen-notify-dispatch.md)
  later removed that wait from the common path without changing the write side.
