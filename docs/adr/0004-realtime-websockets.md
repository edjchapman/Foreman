# ADR 0004 — Realtime job status over WebSockets (Django Channels)

- **Status:** Accepted
- **Milestone:** M4 (realtime slice — completes the half deferred in ADR 0003)
- **Extends:** [ADR 0003](0003-observability.md); the streamed events originate at the same
  claim/terminal/retry/reaper transitions instrumented for logging in ADR 0002/0003.

## Context

Observability (ADR 0003) made every job state-transition *loggable and scrapable*, but a
client still had to **poll** `GET /api/v1/jobs/{id}/` to watch a job move
`PENDING → PROCESSING → SUCCEEDED|FAILED|DEAD_LETTER`. This slice streams those same
transitions live over WebSockets. It is **backend-only and headless-tested**
(`WebsocketCommunicator`); the React consumer is a separate slice. `config/asgi.py` already
carried the placeholder for this, and the reliability state has lived in Postgres since M3
specifically so it could be streamed.

## Decision

### 1. Django Channels + an ASGI `ProtocolTypeRouter`

`config/asgi.py` becomes a `ProtocolTypeRouter`: HTTP stays on the Django app (the DRF API
and `/healthz` `/readyz` `/metrics` are unchanged), WebSocket goes to Channels. We serve it
with **daphne** — the Channels-native ASGI server, which also supplies the ASGI-capable
`runserver` for dev (hence `daphne` must precede `django.contrib.staticfiles` in
`INSTALLED_APPS`). daphne is a single server for both protocols; it **replaces gunicorn**
(WSGI can't do WebSockets), accepting its twisted transitive dependency as the cost of the
one load-bearing serving choice. SSE / long-polling were rejected: they don't model a
persistent per-job subscription as cleanly, and Channels is the idiomatic Django answer.

### 2. Group-per-job fan-out over a Redis channel layer

Each connection joins a group `job.<id>`; a producer broadcasts to that group. The channel
layer is Redis (`channels_redis`), reusing `REDIS_URL` (override: `CHANNELS_REDIS_URL`).
Group-per-job gives targeted delivery with no server-side filtering and works across the
separate worker/beat/web processes — the worker broadcasts, the web process's consumer
receives.

### 3. Serialize on the sync producer; the consumer is a pure passthrough

`jobs.realtime.notify_job` (synchronous, called from Celery task code) is the **only**
sync→async crossing: it re-fetches the row, serializes with `JobSerializer`, and
`async_to_sync(group_send)`s a finished dict. The async `JobStatusConsumer` only re-reads a
snapshot on connect (one `database_sync_to_async` hop) and forwards `job.update` messages.
Crossing the boundary exactly once means the async side never touches the ORM/DRF, so it
can't hit `SynchronousOnlyOperation` — the failure mode that dooms most first Channels
attempts.

### 4. `transaction.on_commit` at the atomic seams

The claim (`_claim_pending`) and the reaper (`_recover_one_lease`) broadcast inside an open
`transaction.atomic()`, so they defer via `transaction.on_commit` — never broadcasting
pre-commit or rolled-back state, and never doing Redis I/O while holding a `SELECT … FOR
UPDATE` row lock. The five autocommit seams call `notify_job` directly.

### 5. Re-fetch-on-connect, then stream deltas (self-healing)

The consumer sends an authoritative snapshot on connect, then deltas; `notify_job` also
re-fetches, so **every** broadcast reflects committed state (this uniformly neutralises the
`_fenced_update`/`_terminal`/progress stale-instance problem — those writes never refresh
the in-memory job). A delta missed in the tiny window between the snapshot read and the
group-join is recovered by a REST poll or reconnect; a re-sent delta is idempotent on the
client.

### 6. Best-effort broadcast — realtime never fails a job

`notify_job` wraps `group_send` in `try/except` and logs `realtime.notify_failed`. Because
the fan-out fires at *terminal* seams, an unguarded broadcast that raised would propagate
out of `process_job` and turn a SUCCEEDED job into a task exception → a spurious retry.
Swallowing keeps the realtime layer strictly **additive** to the reliability model: the UI
can go dark without touching correctness.

### 7. Open + origin-validated

`AllowedHostsOriginValidator` + `AuthMiddlewareStack`, no per-connection auth — parity with
the unauthenticated REST API, with the origin check as the cross-site-WebSocket-hijacking
defence.

### 8. InMemoryChannelLayer in tests

The suite swaps the Redis layer for `InMemoryChannelLayer` (autouse fixture) so it needs no
Redis. Consumer tests use `WebsocketCommunicator` under pytest-asyncio (`auto` mode) and are
**Postgres-only** (`database_sync_to_async` uses a second connection an in-memory SQLite
can't share); the `on_commit` seams are covered synchronously with
`django_capture_on_commit_callbacks`.

## Consequences

- **New runtime deps:** `channels`, `channels-redis`, `daphne` (and the twisted/autobahn
  stack) — the supply-chain surface they add is the price of realtime; **gunicorn is
  dropped** (prod serving moves WSGI→ASGI).
- **`INSTALLED_APPS` ordering constraint** — `daphne` before `staticfiles`, else dev
  WebSockets silently 404.
- **Channel layer needs Redis** (already present for Celery); its outage degrades realtime
  only — jobs still process, and `/readyz` reports the broker down.
- **`/metrics` still has no WebSocket metrics** (connection count, broadcast rate) — the
  counterpart deferred in ADR 0003, still deferred here.
- **Future work:** WebSocket Prometheus metrics; per-connection auth if the API gains auth;
  progress-broadcast backpressure for very large imports; structlog in the consumer; and a
  real-Redis pipeline E2E tier (the current pipeline is verified by composition — seam
  tests + `notify_job` unit + consumer-forward test — plus the React UI slice.)
