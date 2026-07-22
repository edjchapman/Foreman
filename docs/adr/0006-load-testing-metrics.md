# ADR 0006 — Load testing & event-rate / latency metrics

- **Status:** Accepted
- **Milestone:** M6 (prove it under load — incremental, post-M5)
- **Extends:** [ADR 0003](0003-observability.md), whose deferred "event-rate
  counters" and gauge-only tradeoff this decision resolves.

## Context

M1–M5 built and shipped a reliability story — at-least-once delivery, exactly-once
effect, retries/DLQ, lease-based crash recovery — but every claim about *behaviour
under load* (throughput, backpressure, latency at concurrency) was argued in prose
and never measured. Two gaps made measurement impossible:

1. **No load generator.** Nothing exercised the submit → outbox → worker pipeline
   at volume, so the `SKIP LOCKED` dispatch, worker saturation, and backoff were
   never observed under pressure.
2. **Metrics were point-in-time gauges only** ([ADR 0003](0003-observability.md)).
   `/metrics` could answer "how deep is the queue *now*" but not "what is the
   success rate" or "what is p95 processing latency" — there was no `rate()`-able
   counter and no latency distribution.

## Decision

### DB-derived counters and histograms (not process-local, not multiprocess mode)

ADR 0003 rejected process-local `Counter`s because the worker, Beat, and web
server are **separate containers** — a counter incremented in the worker is
invisible at the web-served `/metrics` without `prometheus_client` multiprocess
mode (a shared writable dir, false across containers) or a Pushgateway. That
reasoning is unchanged, so the new metrics stay **derived from Postgres at scrape
time**, extending the existing `ForemanCollector`:

- **`foreman_jobs_processed_total{status}`** — a counter over the *terminal*
  states (`SUCCEEDED`, `FAILED`, `DEAD_LETTER`). A job never leaves a terminal
  state, so the live count of rows in it *equals* the cumulative number that ever
  reached it — a monotonic total recovered from a plain `GROUP BY` query, no
  cross-process state required. Transient states (`PENDING`, `PROCESSING`) are
  excluded precisely because they are not monotonic.
- **`foreman_job_queue_wait_seconds`** (submit → first claim) and
  **`foreman_job_processing_seconds`** (claim → terminal) — histograms bucketed
  **DB-side in one query each** via SQL `FILTER` (`Count(filter=Q(d__lte=bound))`)
  over a `DurationField` expression. Because each bound counts all durations
  below it, the results are already cumulative — the Prometheus histogram wire
  format — and scrape cost stays O(1) queries as the table grows, never pulling
  rows into Python.

The histograms need durable per-phase timestamps, so `Job` gains two nullable,
additive fields — `started_at` (stamped at claim) and `finished_at` (stamped at
the terminal transition). `updated_at` could not serve: it is overwritten on
every save, so it cannot reconstruct a phase duration. The fields are nullable
and backfill-free, keeping the schema forward-compatible per the project
convention.

### Locust over k6 for the harness

The load generator is **Locust** (`load/locustfile.py`), run via `make load` and
retargeted with `FOREMAN_LOAD_URL`, mirroring the `e2e/` suite exactly: its own
dependency group, its own make target, and **excluded from `make ci`** because it
needs a live platform (Redis + Celery workers), not just a database.

Locust is Python and installs into a `uv` dev-group, matching this repo's
Python-only, `uv`-managed, supply-chain-frugal toolchain — the same way Playwright
is vendored for `e2e/`. k6 is a separate Go binary outside `uv`, off-pattern here.
The harness is mostly fire-and-forget: because processing latency and throughput
are now readable at `/metrics`, the generator does not need to observe results
itself — the evidence is scraped server-side.

## Consequences

- **The ADR 0003 tradeoff is resolved.** `rate(foreman_jobs_processed_total[5m])`
  gives throughput and an error ratio; `histogram_quantile(0.95, …)` gives p95
  latency — all without multiprocess mode or a Pushgateway.
- **Counter monotonicity has a documented boundary.** `redrive` returns a
  `DEAD_LETTER` job to `PENDING`, and any future retention pruning deletes
  terminal rows — either can make a `*_total` series *decrease*. Prometheus
  `rate()` tolerates counter resets, so the impact is a minor undercount across a
  reset, not a wrong graph. Consistent with the project's habit of documenting a
  boundary rather than over-engineering it away.
- **Scrape cost grows by two histogram queries.** Each is a single aggregate over
  jobs with both timestamps set; add a partial index on `(finished_at)` if a
  large terminal-row population ever makes the scrape measurable.
- **No new runtime dependency.** The counter/histogram families come from the
  already-present `prometheus-client`; Locust is dev-only, never in the image.
- **Future work:** a per-`job_type` label once more than one job type exists, and a
  shipped Grafana dashboard / Alertmanager rules built on these series. OpenTelemetry
  traces spanning API → outbox → worker → realtime shipped in
  [ADR 0008](0008-opentelemetry-tracing.md).
  *Update (2026-07): the dashboard + alert/SLO rules shipped as the committed
  `observability/` compose profile (`make observe`); see the runbook's
  [SLOs & alerts](../runbook.md#slos--alerts).*
