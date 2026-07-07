# ADR 0008 — OpenTelemetry distributed tracing across the outbox

- **Status:** Accepted
- **Milestone:** M7 (observability depth — incremental, post-M5)
- **Extends:** [ADR 0001](0001-transactional-outbox.md) (the outbox this traces
  *through*), [ADR 0003](0003-observability.md) and [ADR 0006](0006-load-testing-metrics.md)
  (which both deferred "OpenTelemetry traces" to future work).

## Context

The platform emits structured logs (ADR 0003) and DB-derived metrics (ADR 0006), but
neither can answer *"where did this one job spend its time?"* across the pipeline. A
submission crosses four process boundaries — API → outbox relay → worker → realtime — and
logs/metrics see each in isolation. Distributed tracing stitches them into one causal
tree per job. Both ADR 0003 and ADR 0006 named it as the obvious next observability step.

The wrinkle is structural, and it is exactly what makes this worth an ADR: **the
transactional outbox deliberately severs the in-process/broker call chain.** The API
commits a `Job` + `OutboxEvent` and returns; a *separate* process (`dispatch_outbox`, or
the push-dispatch listener of ADR 0007) reads that row *later* and only then emits the
Celery message. OpenTelemetry's auto-instrumentation propagates context through in-process
calls and through the broker message — but it cannot see across a row that is written now
and read minutes later by another process. Left alone, a job would produce two unconnected
traces: one for the request, one for the worker.

## Decision

Auto-instrument the framework hops and add **exactly one manual bridge** across the outbox.

### Auto-instrument the free hops

`DjangoInstrumentor`, `CeleryInstrumentor`, and `PsycopgInstrumentor` give HTTP server
spans, Celery produce/consume spans (with broker-message context propagation), and DB
spans — no per-call code. Setup lives in one seam, `config/otel.py::configure_tracing`,
mirroring how `jobs/metrics.py` centralises the Prometheus wiring.

### Bridge the outbox by persisting the trace context in the row

The `OutboxEvent.payload` is already a `JSONField` (`{"job_id": …}`), so the W3C
`traceparent` rides along under a `"trace"` key — **no schema migration**, keeping the
`Job` schema forward-compatible as the conventions require. Two points, and only two:

1. **Inject at write** (`jobs/services.submit_job`): inside the same `transaction.atomic()`
   that writes the Job and event, `inject_trace()` captures the *active* request context
   into the payload. It commits atomically with the row — a trace context is never
   persisted for a job that didn't, and vice versa.
2. **Re-attach at dispatch** (`jobs/tasks.dispatch_outbox`): a claimed batch mixes events
   from many originating requests, so context is re-hydrated **per event** via
   `span_from_carrier("outbox.dispatch", …)`. Starting that span *current* means Celery's
   instrumentation injects it into the `process_job` message — so the worker span links
   back to the original request with no further code. After this one hop, propagation is
   automatic the rest of the way.

The realtime leg reuses the same primitives: `notify_job` opens a span and injects its
context into the channel-layer message; the consumer re-attaches it for a short `ws.send`
span, completing the tree to the client.

### Per-process `service.name`, env-gated, off by default

Each entrypoint configures with its own name — `foreman-web` (ASGI), `foreman-worker`,
`foreman-beat`, `foreman-listener` — so hops are visible as service boundaries. Config is
gated on `OTEL_ENABLED` (default **false**): CI, the test suite, and any deployment that
doesn't opt in pay zero cost and see no behavioural change. Worker setup runs in Celery's
`worker_process_init` signal, not at import — a `BatchSpanProcessor`'s exporter thread does
not survive `fork()`, so each prefork child must build its own.

### Log ↔ trace correlation

`config/logformat.py` promotes the active `trace_id`/`span_id` onto every JSON log line, so
one query pivots from a job's log event to its trace and back — closing the loop ADR 0003
left open. Absent a span, the keys are simply omitted.

### Export: local Jaeger, vendor OTLP in prod

Local dev runs a Jaeger all-in-one in Compose (UI on `:16686`). Prod exports over OTLP to a
vendor free-tier (Grafana Cloud Tempo, or Honeycomb) — only an endpoint + auth header, so
the backend is swappable via env. Terraform gates it on a supplied endpoint: no endpoint →
tracing stays off, no hosting cost. The endpoint/headers are new sensitive vars fed as
`TF_VAR_*` (gh secrets in CD), following the Railway-token secret pattern.

## Consequences

- **One connected trace per job**, API → `outbox.dispatch` → worker → `ingest` →
  `notify_job` → `ws.send`, spanning distinct services — the causal view logs and metrics
  couldn't give. Proven by a hermetic test asserting a shared `trace_id` and parent chain
  across the outbox boundary (`jobs/tests/test_tracing.py`).
- **No migration, no schema change.** The bridge rides the existing JSON payload.
- **Zero cost when off.** The helpers degrade to no-ops (`inject_trace() → {}`, non-recording
  spans); the whole default suite runs unchanged at the 90 % coverage floor.
- **New runtime dependencies** — the OTel SDK + three instrumentation packages. Load-bearing
  like `prometheus-client` (ADR 0003); justified by the cross-cutting visibility, and inert
  unless enabled.
- **Head sampling is parent-based** (`OTEL_SAMPLER_RATIO`, default 1.0 for the low-traffic
  demo). A sampled request keeps its whole tree; lower the ratio if volume — and vendor
  cost — grows.
- **A lost outbox row loses its trace with it** — acceptable, and symmetric with ADR 0007:
  the job itself is still recovered by the reaper/Beat backstop, just untraced.
- **Future work:** span-based SLO alerting, and extending `service.version` into a
  deploy-marker overlay once a dashboard exists.
