# Architecture Decision Records

Minimal records of *that* a decision was made and *why*: a 1–3 sentence core,
plus **Considered Options** only where the rejected path is worth remembering
and **Consequences** only where non-obvious. An ADR earns its file when the
decision is hard to reverse, surprising without context, and the result of a
real trade-off. Newer decisions amend or supersede older ones explicitly
rather than by edit; the project's vocabulary lives separately in
[`CONTEXT.md`](../../CONTEXT.md).

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-transactional-outbox.md) | Transactional outbox for job dispatch | Accepted |
| [0002](0002-retries-dlq-lease.md) | Retries, dead-letter, and lease-based crash recovery | Accepted |
| [0003](0003-observability.md) | Observability: structured logging, metrics, health/readiness | Accepted |
| [0004](0004-realtime-websockets.md) | Realtime job status over WebSockets (Django Channels) | Accepted — amended by 0009 |
| [0005](0005-deployment-platform.md) | Deployment platform: Railway, one image, semver-pinned CD | Accepted |
| [0006](0006-load-testing-metrics.md) | Load testing & event-rate / latency metrics | Accepted |
| [0007](0007-listen-notify-dispatch.md) | LISTEN/NOTIFY push-dispatch for the outbox | Accepted |
| [0008](0008-opentelemetry-tracing.md) | OpenTelemetry distributed tracing across the outbox | Accepted |
| [0009](0009-lifecycle-module.md) | The lifecycle module owns every Job transition | Accepted |
