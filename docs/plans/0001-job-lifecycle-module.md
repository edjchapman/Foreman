# Plan 0001 — `jobs/lifecycle.py` deep module

- **Status:** Implemented — [PR #150](https://github.com/edjchapman/Foreman/pull/150),
  merged 2026-08-10 (squash commit `7e2b2ed`)
- **Motivated by:** [design review 2026-08](../design-review-2026-08.md),
  recommendation A (findings 1, 2, 4)
- **Approved:** 2026-08-10, after a decision-by-decision grilling session
- **Amends:** [ADR 0004](../adr/0004-realtime-websockets.md) (broadcast ownership)

## Context

The `Job` state machine (PENDING → PROCESSING → SUCCEEDED | FAILED |
DEAD_LETTER, plus retry/reap/redrive edges) had no module of its own —
transitions were spelled across ~10 sites in `tasks.py`/`services.py`, the
lease-reset field set was written out 4 times, the dead-letter rule twice, and
broadcasting (`notify_job`) was a per-caller obligation that `admin.py` and
`manage.py redrive` had already forgotten (silent redrives). The best tests in
the suite could only express themselves against private names.

Fix: one deep module, [`jobs/lifecycle.py`](../../jobs/lifecycle.py), owning
every `Job` write and its side effects (persist + log + broadcast).

## Agreed decisions

Settled one at a time before implementation; each was a genuine fork:

1. **Single PR** — code motion + broadcast-ownership change together (motion
   without the broadcast move just relocates the caller obligation).
2. **`redrive` moves wholly into lifecycle** — no shim; `services.py` keeps only
   `submit_job` (entry into the machine).
3. **`record_progress` included** — every observable `Job` write goes through
   lifecycle, so `notify_job`'s production callers shrink to lifecycle +
   `submit_job`.
4. **Requeue scan splits** — lifecycle owns lock + visibility-push
   (`claim_due_for_requeue`), `tasks.py` publishes *after commit* (a deliberate
   durability improvement over the previous publish-inside-transaction).
5. **Locking helper becomes a shared custom QuerySet** on `Job`/`OutboxEvent`
   (Django-idiomatic, per explicit steer to follow Django best practice).
6. **Creation broadcasts** — `submit_job` gains an on-commit `notify_job`, so
   the push-only queue board shows PENDING arrivals (previously invisible until
   claim — exactly wrong during a backlog).
7. **Fenced transitions return `bool`** — `rows == 0` → `False`, no broadcast
   (the live owner announces its own writes), truthful logging instead of a
   false `job.succeeded`.
8. **Transition logs move with the transitions** — lifecycle emits the
   `job.*` events; `tasks.py` keeps scan-level `recover.*` counts.
9. **Tests repoint in place** (suite is behaviour-named); new contract tests in
   `test_lifecycle.py`; `test_broadcast_seams.py` deleted.

An accuracy pass (locking + durability) added three refinements: `redrive`
uses a *blocking* row lock and captures matched ids so its per-job broadcasts
and count are truthful; the requeue split's publish-after-commit ordering is
documented as intentional; and a fenced-out progress write makes the worker
abort early instead of running as a zombie.

## Planned public interface

```python
claim(job_id) -> Job | None                 # PENDING→PROCESSING, fresh lease
succeed(job, *, result) -> bool             # fenced terminal write
fail(job, *, error) -> bool                 # fenced terminal (poison input)
retry_or_dead_letter(job, *, error, delay_seconds=None) -> str
reap_expired_leases() -> int
redrive(job_ids) -> int                     # from services.redrive_dead_letter
record_progress(job, percent) -> bool       # fenced progress write + broadcast
claim_due_for_requeue() -> list[Job]        # lock + visibility push; caller publishes
retry_delay(attempts) -> float              # public backoff curve
```

Invariant: every client-observable `Job` write commits first, then schedules
its own on-commit broadcast (best-effort, at-most-once — DB state is the
truth) and emits its own structured log event. Callers never notify.

## Verification plan

`make ci` (ruff + mypy strict + coverage-gated pytest), `make preflight`,
the Postgres concurrency suite, and a live check: submit on the demo stack and
watch the queue board receive the job at *submission*; redrive a dead-letter
job from the CLI and see the flip arrive on an open socket.

## Outcome

Shipped as planned, in four commits on [PR #150](https://github.com/edjchapman/Foreman/pull/150).
Deviations and learnings:

- **Interface grew two log-fidelity parameters**: `fail`/`retry_or_dead_letter`
  take `error_class=` so the transition can log what only the caller knows
  (the exception type). The reaper's dead-letter log changed schema as a
  result: `reason="lease expired"` → `error_class="LeaseExpired"`; `redrive`
  gained a new `job.redriven` event. No in-repo log consumer keyed on the old
  field.
- **The fenced-progress abort needed a vehicle**: a private `_FencedOutError`
  in `tasks.py` carries "stop importing, you were reaped" out of the chunk
  loop; `process_job` reports it as a distinct `"fenced"` outcome.
- **The queryset gained a second method**: `LockingQuerySet.lock()` (blocking)
  alongside `lock_for_claim()` (SKIP LOCKED), because redrive must never
  silently under-count concurrently locked rows. Typing under mypy strict was
  a non-event — the django-stubs plugin handled `.as_manager()` on a PEP 695
  generic QuerySet first try.
- **Coverage needed two follow-up commits**: the fence branches and the
  backend-capability fallbacks (which no single backend can execute) were
  uncovered diff lines until tests pinned them via monkeypatched
  `connection.features`.
- **The review pass caught a docs gap the checklist missed**: ADR 0004 still
  described caller-side broadcasting; fixed as a dated amendment rather than a
  rewrite.
- Live verification worked as specified — the firehose frame sequence showed
  the new creation broadcast (`PENDING` before any claim) and the previously
  silent CLI redrive producing a `PENDING:att0` frame.
