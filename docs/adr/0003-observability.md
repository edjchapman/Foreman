# Observability: structured logging, metrics, health/readiness

Every job transition emits one structured JSON log event (stdlib formatter,
`src/config/logformat.py`); `/metrics` exposes **gauges derived from Postgres at
scrape time** (queue depths, backlog age) rather than process-local counters;
and `/healthz` (pure liveness, no dependency I/O) is split from `/readyz`
(database + broker), because a DB blip should stop traffic routing, not restart
every pod.

## Considered Options

- **structlog** — the log surface is one module with ~7 call sites; contextvar
  binding buys nothing there, and the project is deliberately dependency-frugal.
- **Process-local Prometheus counters** — broken for this topology: worker,
  Beat, and web are separate containers, so a worker-incremented counter is
  invisible to the web-served `/metrics` without multiprocess mode (needs a
  shared writable dir) or a Pushgateway.

## Consequences

- Gauges can't express event *rates*; that gap was accepted here and resolved
  later by [ADR 0006](0006-load-testing-metrics.md)'s DB-derived counters.
- `CELERY_WORKER_HIJACK_ROOT_LOGGER = False` is load-bearing — without it
  Celery reformats the worker's logs, and the worker emits most job events.
- Anything monitoring `/healthz` for database health must move to `/readyz`.
