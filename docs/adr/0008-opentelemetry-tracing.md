# OpenTelemetry distributed tracing across the outbox

Auto-instrumentation (Django, Celery, psycopg) covers the free hops, but the
transactional outbox *deliberately severs the call chain* — a row written now
is read minutes later by another process, which no propagation can cross. So
there is exactly **one manual bridge**: `submit_job` injects the W3C
`traceparent` into the outbox event's JSON payload (atomically with the row),
and dispatch re-hydrates it per event, making the worker span link back to the
originating request. Tracing is env-gated (`OTEL_ENABLED`, default off — zero
cost when disabled), each process exports under its own `service.name`, and
`trace_id`/`span_id` are promoted onto every JSON log line for log↔trace pivots.

## Consequences

- One connected trace per job — API → dispatch → worker → ingest → broadcast →
  `ws.send` — across four processes, with no schema migration (the context
  rides the existing JSON payload).
- Worker tracing must initialize in Celery's `worker_process_init` signal: a
  `BatchSpanProcessor`'s exporter thread does not survive `fork()`.
- A lost outbox row loses its trace with it — symmetric with
  [ADR 0007](0007-listen-notify-dispatch.md): the job is still recovered, just untraced.
- New runtime dependencies (OTel SDK + three instrumentors) — inert unless enabled.
