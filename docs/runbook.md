# Runbook

Operating guide for Foreman's job pipeline: what the endpoints and metrics mean, how to
read the logs, and what to do when something goes wrong. The *why* lives in the
[ADRs](adr/README.md); this is the operational how-to.

## Services

- **web** — the DRF API + WebSocket stream; serves `/healthz`, `/readyz`, `/metrics`, and `ws/jobs/<id>/`.
- **worker** — Celery worker running `process_job` (the CSV import and terminal state).
- **beat** — Celery Beat; drives two pollers, `dispatch_outbox` (~1s) and `recover_jobs` (~5s).

All three share one image and one Postgres + Redis. Job state lives in Postgres, not the
broker, so it stays queryable and survives a broker restart.

## Endpoints

| Endpoint | Kind | Meaning | On failure |
|---|---|---|---|
| `GET /healthz` | Liveness | Process can serve requests (no dependency I/O). | Orchestrator **restarts** the pod. |
| `GET /readyz` | Readiness | Database **and** broker reachable; `503` otherwise. | Orchestrator **stops routing** traffic (does not restart). |
| `GET /metrics` | Metrics | Prometheus exposition of the gauges below. | — |
| `WS /ws/jobs/{id}/` | Realtime | Snapshot on connect, then live status/progress deltas. | Closes `4404` for an unknown job; a channel-layer outage stops updates (jobs unaffected) — clients poll the REST endpoint. |

Liveness and readiness are deliberately distinct — a DB or broker blip must not restart
pods. See [ADR 0003](adr/0003-observability.md).

## Metrics

All gauges, computed from the database at scrape time (prefix `foreman_`):

| Metric | Meaning | Watch for | Action |
|---|---|---|---|
| `foreman_jobs{status}` | Jobs in each status. | `status="DEAD_LETTER"` > 0 and rising. | Investigate the cause, then [redrive](#redrive-a-dead-lettered-job). |
| `foreman_outbox_pending` | Undispatched outbox events. | Sustained > 0. | Relay not dispatching — see [the relay is behind](#the-relay-is-behind). |
| `foreman_outbox_oldest_pending_age_seconds` | Age of the oldest undispatched event (dispatch lag). | > ~30s. | Beat's `dispatch_outbox` is not running — see [the relay is behind](#the-relay-is-behind). |
| `foreman_jobs_retry_scheduled` | PENDING jobs waiting on backoff. | Sustained growth. | Systemic transient failure (a dependency is down); check `job.retry_scheduled` logs. |
| `foreman_jobs_processing_oldest_age_seconds` | Age of the oldest in-flight job. | > `JOB_LEASE_SECONDS` and climbing. | The reaper (`recover_jobs`) is not running, or jobs are genuinely stuck. |

Example alert expressions (PromQL):

```promql
foreman_jobs{status="DEAD_LETTER"} > 0
foreman_outbox_oldest_pending_age_seconds > 30
foreman_jobs_processing_oldest_age_seconds > 300
```

## Realtime (WebSockets)

Clients stream a single job at `ws/jobs/<id>/` — an authoritative snapshot on connect, then
status/progress deltas. Fan-out rides the same transitions as the logs (see `jobs.realtime`).
The channel layer is Redis (`CHANNELS_REDIS_URL`, default `REDIS_URL`); broadcasts are
**best-effort**, so a channel-layer outage stops updates but never fails a job — clients fall
back to `GET /api/v1/jobs/{id}/`. Sanity-check a running stack with `websocat`:

```bash
websocat ws://localhost:8000/ws/jobs/<id>/
```

There are no WebSocket metrics yet (deferred — see [ADR 0004](adr/0004-realtime-websockets.md)).

## Reading the logs

Logs are one JSON object per line. `event` is a stable name; correlate a job across events
by `job_id`.

```bash
# every dead-letter, pretty-printed
docker compose logs worker | jq 'select(.event == "job.dead_letter")'

# follow one job through claim -> retry -> terminal
docker compose logs worker | jq 'select(.job_id == "<id>")'
```

Event names: `job.claimed`, `job.succeeded`, `job.failed` (permanent), `job.retry_scheduled`,
`job.dead_letter`, plus `recover.requeued` / `recover.reaped` from the recovery scan and
`realtime.notify_failed` if a WebSocket broadcast is dropped.

## Failure taxonomy

Foreman separates **permanent** failures (poison input — an `IngestError` goes straight to
`FAILED`, never retried) from **transient** ones (anything else — a backoff retry, then
`DEAD_LETTER`). The crash-window analysis (broker redelivery vs the lease reaper), the
lease-reclaim race, and the residual stuck-`PENDING` window are all documented in
[ADR 0002](adr/0002-retries-dlq-lease.md) — read it before changing retry or lease
behaviour.

## Procedures

### Redrive a dead-lettered job

A `DEAD_LETTER` job is one that exhausted `JOB_MAX_ATTEMPTS`. After fixing the root cause,
return it to the queue:

```bash
uv run python manage.py redrive <job_id> [<job_id> ...]
```

This resets it to `PENDING` (fresh `attempts`, `available_at = now`); the `recover_jobs`
requeue lane re-dispatches it. A Django-admin action ("Redrive") does the same for a
selected set. Redrive refuses any job that is not `DEAD_LETTER`.

### The relay is behind

If `foreman_outbox_pending` or its oldest-age climbs, Beat's `dispatch_outbox` poller is
not running. Confirm the `beat` service is up; for a one-shot manual dispatch without Beat:

```bash
make relay   # dispatch the outbox once
```

### A worker crashed mid-job

No action needed — recovery is automatic. A crashed worker's job stays `PROCESSING` until
its lease expires (`JOB_LEASE_SECONDS`), then `recover_jobs` reclaims it to `PENDING` (or
`DEAD_LETTER` if attempts are spent). A slow-but-alive worker that resumes after being
reaped is fenced out by its stale `lease_token` and cannot corrupt the row.

### Tunables

Env-overridable; defaults shown. See [ADR 0002](adr/0002-retries-dlq-lease.md) for the
reliability tunables' rationale.

| Variable | Default | Controls |
|---|---|---|
| `JOB_MAX_ATTEMPTS` | 3 | Attempts (incl. the first) before dead-letter. |
| `JOB_RETRY_BASE_SECONDS` | 2 | Backoff base; the ceiling doubles per attempt. |
| `JOB_RETRY_MAX_SECONDS` | 300 | Backoff cap. |
| `JOB_LEASE_SECONDS` | 120 | Worker lease TTL while `PROCESSING`. |
| `RECOVER_POLL_SECONDS` | 5 | How often `recover_jobs` runs. |
| `DJANGO_LOG_FORMAT` | `json` | Log output format: `json` or `console`. |
| `CHANNELS_REDIS_URL` | `REDIS_URL` | Redis for the WebSocket channel layer. |

## Production configuration

The app is 12-factor: prod is configured entirely by env vars, and the hardening settings are
**opt-in** (so dev and tests stay simple). Set these when deploying behind HTTPS:

| Variable | Set to | Why |
|---|---|---|
| `DJANGO_DEBUG` | `false` | Never serve a public site with `DEBUG=true`. |
| `DJANGO_SECRET_KEY` | a long random secret | 50+ chars, ≥5 distinct; `check --deploy` rejects the dev default. |
| `DJANGO_ALLOWED_HOSTS` | your domain | Also gates the WebSocket `Origin` check. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://<domain>` | Admin login + the demo page POST. |
| `DJANGO_SECURE_SSL_REDIRECT` | `true` | Redirect HTTP → HTTPS. |
| `DJANGO_SECURE_COOKIES` | `true` | `Secure` flag on session + CSRF cookies. |
| `DJANGO_SECURE_HSTS_SECONDS` | `31536000` | Enable HSTS (+ subdomains + preload). |

`SECURE_PROXY_SSL_HEADER` is set unconditionally so Django trusts a TLS-terminating proxy's
`X-Forwarded-Proto` header — this **must** be in place before `SECURE_SSL_REDIRECT`, or the
already-HTTPS request is seen as HTTP and 301-loops forever. Static files are collected into
the image and served by WhiteNoise. Validate with `python manage.py check --deploy`; run
`migrate` as a release step (not per web replica).
