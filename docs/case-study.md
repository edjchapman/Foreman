# Case study — reliability engineering beyond CRUD

The engineering story behind foreman: how a deliberately small feature — import
a property CSV, hand back a report — earns **at-least-once delivery,
exactly-once *effect*, failure isolation, and autonomous crash recovery**, and
how each of those claims is tested, observed, and shipped. The system is live:
[**foreman-demo.up.railway.app**](https://foreman-demo.up.railway.app).

Every decision below was recorded as an [ADR](adr/README.md) at the moment it
was made; this document is the connective tissue.

## The premise

Most demo backends stop where the happy path ends: a request writes a row, a
worker picks it up, a page shows the result. foreman inverts the usual ratio —
**minimal feature surface, maximal guarantees**. The feature is one CSV import
pipeline; the engineering is everything that can go wrong between "202
Accepted" and "here's your report", closed window by window:

```text
POST /api/v1/jobs/ → transactional outbox → relay → idempotent worker
     (atomic)         (at-least-once)              (exactly-once effect,
                                                    retries, DLQ, lease)
   → live WebSocket status → streamed CSV report
```

## Never lose a job: the transactional outbox

Submitting a job must write PostgreSQL *and* enqueue Celery work — two systems,
no shared transaction. Write-then-enqueue loses the job if the process dies in
between; enqueue-then-write hands workers a phantom. This is the classic
**dual-write problem**, and "just call `task.delay()` in the view" quietly has
it.

foreman writes the `Job` and an `OutboxEvent` in **one database transaction**;
a relay (Celery Beat) polls PENDING events, publishes, and marks them
DISPATCHED. Multiple relay instances claim disjoint batches with
`SELECT … FOR UPDATE SKIP LOCKED`, and the event carries a payload snapshot so
the relay never re-reads — and so never races — the job. The price is honest:
delivery becomes **at-least-once** (a crash between publish and commit
redelivers), which sets up the next problem. ([ADR 0001](adr/0001-transactional-outbox.md))

## Never double an effect: idempotent workers

At-least-once delivery means the worker owns exactly-once *effect*. Two
mechanisms, at different layers:

- **A state guard** — `process_job` claims PENDING → PROCESSING under a row
  lock and no-ops on anything non-PENDING, so a redelivered message doesn't
  reprocess an in-flight job.
- **A data constraint** — imported rows carry a per-job natural key
  (`UniqueConstraint(job, external_id)`) and land via
  `bulk_create(ignore_conflicts=True)`, so even a genuine double-run converges
  on the same rows instead of duplicating them.

A subtle consequence: `result["rows_imported"]` reports **target state** (rows
that exist for the job), not rows-inserted-this-run — the only semantic that
stays truthful under redelivery.

## Fail differently: taxonomy, backoff, dead-letter

A transient database blip and a malformed CSV deserve opposite treatment, so
`process_job` classifies failures: **poison input** (the `IngestError` family)
goes straight to FAILED with no retries; **anything else** retries with capped,
full-jitter exponential backoff until `JOB_MAX_ATTEMPTS`, then lands in
DEAD_LETTER for an operator to inspect and `redrive`.

Two decisions here are worth defending:

- **Retry state lives in Postgres, not the broker.** Celery's native
  `self.retry()` is not just off-theme — it's *broken against this design*: it
  redelivers while the job is still PROCESSING, the state guard skips it, and
  the job strands. Database-driven retries (`attempts`, `available_at`) are
  queryable, survive a broker restart, and later turned out to be exactly what
  the realtime layer needed to stream.
- **Two dispatch lanes, provably disjoint.** The outbox relay dispatches new
  jobs; a second Beat scan dispatches due retries. They partition on
  `available_at` NULL-ness, so no job is ever dispatched by both or dropped by
  both. ([ADR 0002](adr/0002-retries-dlq-lease.md))

## Survive crashes: lease, reaper, fencing token

The state guard creates its own failure mode: if a worker dies *after*
claiming, the broker can't redeliver (the guard blocks it) and the job would
sit PROCESSING forever. Each claim therefore takes a **lease**
(`leased_until`); a reaper scan returns expired-lease jobs to PENDING (or
DEAD_LETTER if attempts are spent). Broker-level recovery (`acks_late` +
`reject_on_worker_lost`) covers the *other* crash window:

| Crash window | Job state | Recovered by |
|---|---|---|
| Before the claim commits | PENDING | broker redelivery |
| After the claim commits | PROCESSING | the lease reaper |

And because a reaped worker might be slow rather than dead, every claim stamps
a fresh **fencing token**: a late write from the original worker carries a
stale token, matches zero rows, and is discarded instead of clobbering the
re-claimed job. The natural-key constraint independently absorbs any rows the
brief double-run inserted.

One residual window is **documented rather than patched**: a brand-new job
whose dispatch message is permanently lost after its event was marked
DISPATCHED is recovered only by broker `acks_late`/visibility-timeout. That's
the irreducible boundary of an at-least-once system — knowing where the
machinery ends matters as much as the machinery.

## See it run: observability before UI

Reliability machinery you can't interrogate is a liability, so observability
landed *before* the realtime UI. Three pieces
([ADR 0003](adr/0003-observability.md)):

- **Structured JSON logs** — every state transition emits one JSON object with
  a stable event token (`job.dead_letter`, `job.retry_scheduled`), via a
  ~25-line stdlib `Formatter`. structlog was considered and rejected: seven
  call sites don't earn a dependency under this repo's supply-chain scrutiny.
- **DB-derived Prometheus gauges** — `/metrics` computes queue depths and ages
  from Postgres at scrape time. Process-local counters are *broken for this
  topology*: the worker that increments them and the web process that serves
  `/metrics` are different containers. Depth-plus-age from the database is
  cross-process-true with zero extra machinery.
- **Liveness/readiness split** — `/healthz` never does dependency I/O;
  `/readyz` checks DB + broker. A database blip should stop traffic routing,
  not restart every pod. `/readyz` later became the deploy gate.

## Watch it live: WebSockets without the usual failure modes

Job state streams to `ws/jobs/<id>/` via Django Channels — snapshot on
connect, then deltas ([ADR 0004](adr/0004-realtime-websockets.md)). Three
rules keep the notorious Channels pitfalls out:

- **Exactly one sync→async crossing.** The synchronous `notify_job` (called
  from task code) re-fetches, serializes, and `group_send`s a finished dict;
  the async consumer never touches the ORM, so `SynchronousOnlyOperation`
  can't happen.
- **`transaction.on_commit` at the atomic seams** — no broadcast of
  uncommitted state, no Redis I/O while holding a row lock.
- **Broadcast is best-effort.** `notify_job` swallows and logs failures,
  because the alternative is worse than a dark UI: an exception at a terminal
  seam would turn a SUCCEEDED job into a spurious retry. The realtime layer is
  strictly additive to the reliability model.

The demo page (and the E2E suite) verify the point end-to-end: status arrives
over the socket, and the tests **assert the page never polls**.

## Finish the story: the streamed report

`GET /api/v1/jobs/{id}/report/` streams the imported records as CSV. Under
ASGI this detail is load-bearing: a `StreamingHttpResponse` fed by a *sync*
iterator makes Django buffer the whole body (and warn); the report instead
feeds an **async generator over `aiterator()`**, so a large import streams
row-by-row from the database to the socket.

## Prove it: verification at every layer

- **CI runs against real PostgreSQL** (90% coverage floor) because the locking
  guarantees (`SKIP LOCKED` claims, second-connection consumer tests) are
  Postgres-runtime properties; a feature-guarded SQLite path keeps local runs
  fast without forking the settings.
- **The async seams are tested at the right altitude** — `on_commit`
  broadcasts with `django_capture_on_commit_callbacks`, consumers with
  `WebsocketCommunicator`, tasks inline via an eager-Celery fixture.
- **pytest runs strict** — unknown markers and *any* warning fail CI.
- **Playwright E2E against the live platform** (`make e2e`) verifies the
  deployed system: a sample import goes SUCCEEDED over the WebSocket (no
  polling), the CSV downloads, and a poison job fails without retries.

## Ship it: the same rigor, operationalized

Releases are automated end-to-end: Conventional Commits → release-please →
a GHCR image with **SLSA build provenance** → a deploy that pins the **exact
semver tag** on Railway (never `:latest`), web first — its pre-deploy
`migrate` and `/readyz` healthcheck gate the fleet, then worker and beat are
each polled to success so a crash-looping service fails the rollout. The
platform itself is Terraform-declared, and `terraform destroy`/`apply` is the
demo's off/on switch (usage-billed, ~$8–12/mo).
([docs/ci.md](ci.md) · [docs/deploy.md](deploy.md) ·
[ADR 0005](adr/0005-deployment-platform.md))

## Prove it under load: measured, not asserted

Every claim above is about behaviour under pressure, so the pressure has to be
real. A [Locust harness](load-testing.md) drives the actual submit → outbox →
worker pipeline at volume, and the observability layer grew the series needed to
*read* it: a monotonic `foreman_jobs_processed_total{status}` counter
(`rate()` → throughput and error ratio) and queue-wait / processing-latency
**histograms** (`histogram_quantile` → p95). Both stay **DB-derived** — the same
cross-process reasoning as the gauges: worker, Beat, and web are separate
containers, so counts and buckets are recovered from Postgres at scrape time (the
histograms bucket DB-side in one query via SQL `FILTER`), never from
process-local state. This closes the tradeoff ADR 0003 left open — rate counters
without multiprocess mode or a Pushgateway. The counter's one honest boundary:
`redrive` and retention pruning can dent monotonicity, which `rate()` tolerates
as a reset. ([ADR 0006](adr/0006-load-testing-metrics.md))

## What I'd build next

In rough order of what the current design already anticipates:
`LISTEN/NOTIFY` to replace outbox polling (the write path wouldn't change),
OpenTelemetry traces spanning API → outbox → worker → realtime, WebSocket metrics
and per-connection auth, and remote CSV sources (`s3://`, `https://`) behind the
existing ingest seam.

---

*Built by [Ed Chapman](https://github.com/edjchapman). The pipeline in one
sitting: [live demo](https://foreman-demo.up.railway.app) · [ADR index](adr/README.md) ·
[runbook](runbook.md) · [CI/CD pipeline](ci.md) · [load testing](load-testing.md).*
