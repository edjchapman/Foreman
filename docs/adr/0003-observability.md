# ADR 0003 — Observability: structured logging, metrics, health/readiness

- **Status:** Accepted
- **Milestone:** M4 (observability slice — the realtime UI half is separate)
- **Extends:** [ADR 0002](0002-retries-dlq-lease.md), whose "documented failure modes"
  are the seed of the [runbook](../runbook.md) this decision operationalises.

## Context

M3 left the system reliable but **opaque**: logging was ad-hoc `%`-formatted lines in
`process_job`, there were no metrics, and a single `/healthz` mixed liveness with a
database check. An operator could not answer "how deep is the dead-letter queue?", "is the
relay falling behind?", or "why did the orchestrator just restart the web pod?" without
reading code or querying the database by hand. This slice makes the reliability machinery
**observable and operable** — deliberately before the realtime UI is built on top of it.

## Decision

### Structured JSON logging (stdlib, no new dependency)

Every job state transition emits one JSON object — an `event` name (a stable dotted token
like `job.dead_letter`), `job_id`, `attempts`, and event-specific fields (`latency_ms`,
`retry_in_s`, `error_class`) — via a ~25-line `logging.Formatter` subclass
(`config/logformat.py`) wired into Django's `LOGGING`. `DJANGO_LOG_FORMAT=console` swaps to
human-readable lines for local dev.

We chose stdlib over **structlog**: the log surface is a single module (~7 call sites), so
structlog's contextvar binding buys little, and the project is deliberately
dependency-minimal under heavy supply-chain scrutiny (Scorecard, SLSA, dependency-review).
`CELERY_WORKER_HIJACK_ROOT_LOGGER = False` is essential — otherwise Celery reformats the
worker's logs, and the worker is the process that emits most job events. Revisit structlog
in the realtime half, where request/consumer-scoped context actually pays off.

### DB-derived Prometheus gauges (not process counters)

`/metrics` exposes **gauges computed from Postgres at scrape time** — jobs-by-status
(including dead-letter depth), the outbox backlog and its oldest-age (dispatch lag), the
retry-scheduled queue depth, and the oldest in-flight job's age — via a custom collector on
a dedicated `CollectorRegistry`.

The alternative, process-local `Counter`s incremented in `process_job`, is **broken for
this topology**: the Celery worker and Beat are separate processes from the web server that
serves `/metrics`, so a worker-incremented counter is invisible there without
`prometheus_client` multiprocess mode (a shared writable dir — false across separate
containers) or a Pushgateway. DB-derived gauges reflect true cross-process state with none
of that machinery, and depth-plus-age is exactly the golden-signal set for a queue. The one
new runtime dependency, `prometheus-client`, is load-bearing (no reasonable stdlib
substitute) — which is the bar a new dependency has to clear here.

**Tradeoff:** gauges cannot express event *rates* (retries/sec, dead-letters/sec for
`rate()`). Rate counters are deferred until there is a reason to add multiprocess mode or a
Pushgateway; the DB gauges already surface every actionable condition (see the runbook).

### Liveness / readiness split

`/healthz` is now **pure liveness** — 200 whenever the process can serve, with no
dependency I/O. `/readyz` is **readiness** — it checks the database and the Celery broker
and returns 503 if either is unreachable. This is the standard Kubernetes distinction: a
dependency outage should make the orchestrator *stop routing traffic* (readiness), not
*restart the pod* (liveness) — a DB blip that restarts every pod is a self-inflicted
outage. The broker is pinged through the Celery connection (`celery_app.connection()`), so
no dependency is added (Redis is only a transitive dep, via `celery[redis]`).

## Consequences

- **`/healthz` behaviour change.** It no longer returns 503 when the database is down —
  that condition now surfaces on `/readyz`. The compose `web` healthcheck points at
  `/readyz`; any external monitor keyed on `/healthz` for DB health must move to `/readyz`.
- **`/metrics` is unauthenticated-but-safe** (read-only aggregate counts, no PII), like the
  health probes. In a real cluster it would be bound to the internal scrape network.
- **Scrape cost** is ~5 lightweight aggregate queries per scrape, all on indexed filters
  (`job_retry_due_idx`, `outbox_pending_idx`, `Job.status`) — negligible at 15–60s scrape
  intervals.
- **One new runtime dependency** (`prometheus-client`); logging and readiness add none.
- **Future work:** event-rate counters (multiprocess / Pushgateway), OpenTelemetry traces,
  and Channels/WebSocket metrics land with the M4 realtime half.
