# Retries, dead-letter, and lease-based crash recovery

Retry state lives in **Postgres** (`attempts` / `available_at` / `leased_until` /
`lease_token`), never the broker: failures split into permanent (`IngestError`
poison input → FAILED, never retried) and transient (jittered exponential
backoff until `attempts >= JOB_MAX_ATTEMPTS` → DEAD_LETTER). A claim takes a
time-bounded **lease**; a Beat scan reaps expired leases (crashed workers) back
into the retry flow, and every subsequent write is **fenced** on the lease
token so a reaped-but-still-running worker's stale write matches zero rows.
Operators return dead-letter jobs to the pipeline with **redrive** (fresh
attempts budget, dispatched by the existing requeue lane — no new dispatch path).

## Considered Options

- **Celery native `self.retry()`** — broken against this design: it redelivers
  while the job is still PROCESSING, so the claim's PENDING-guard skips it and
  the job strands. Also invisible to SQL and lost on a broker restart.
- **Re-emitting a retry `OutboxEvent`** (one dispatch lane) — grows the outbox
  per retry, muddies "an event is one `job.created` fact", and still can't
  express "the worker died mid-process".

## Consequences

- Two dispatch lanes partition on `available_at` NULL-ness (new jobs → outbox;
  due retries → requeue scan), so no job is dispatched by both or dropped by both.
- The irreducible at-least-once boundary is documented, not engineered away: a
  brand-new job whose dispatched message is permanently lost is recovered only
  by broker `acks_late`/visibility timeout.
- A reaped-then-resumed **zombie worker** can race its replacement; the fence
  discards its state writes and the per-job unique constraint absorbs its rows.
- Tunables (`JOB_MAX_ATTEMPTS`, backoff base/cap, lease and poll intervals) are
  env-overridable; defaults live in `config/settings.py`.
