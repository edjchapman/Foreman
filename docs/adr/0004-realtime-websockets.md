# Realtime job status over WebSockets (Django Channels)

status: accepted — amended 2026-08 (broadcast ownership moved; see [ADR 0009](0009-lifecycle-module.md))

Job transitions stream live over Channels: daphne serves HTTP + WebSocket from
one process (replacing gunicorn — WSGI can't do WebSockets), each connection
joins a `job.<id>` group (plus a `queue` firehose) on a Redis channel layer,
and `jobs.realtime.notify_job` is the **only** sync→async crossing — it
re-fetches the committed row, serializes synchronously, and `group_send`s a
finished dict, so the async consumer never touches the ORM. Clients get an
authoritative snapshot on connect, then deltas; broadcasts are best-effort
(logged and swallowed) so realtime can never fail a job.

Since the 2026-08 amendment, *calling* `notify_job` is not a per-seam
obligation: every transition schedules its own on-commit broadcast inside
`src/jobs/lifecycle.py` ([ADR 0009](0009-lifecycle-module.md)) — correct in both
transactional and autocommit contexts, and impossible for a caller to forget.

## Considered Options

- **SSE / long-polling** — don't model a persistent per-job subscription as
  cleanly; Channels is the idiomatic Django answer.

## Consequences

- daphne must precede `django.contrib.staticfiles` in `INSTALLED_APPS`, or dev
  WebSockets silently 404.
- A Redis channel-layer outage degrades realtime only; jobs still process.
- Consumer tests are Postgres-only (`database_sync_to_async` needs a second
  connection an in-memory SQLite can't share).
