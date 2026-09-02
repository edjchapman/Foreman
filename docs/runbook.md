# Runbook

Operating guide for Foreman's job pipeline: what the endpoints and metrics mean, how to
read the logs, and what to do when something goes wrong. The *why* lives in the
[ADRs](adr/README.md); this is the operational how-to.

## Services

- **web** — the DRF API + WebSocket stream; serves `/healthz`, `/readyz`, `/metrics`, and `ws/jobs/<id>/`.
- **worker** — Celery worker running `process_job` (the CSV import and terminal state).
- **beat** — Celery Beat; drives two pollers, `dispatch_outbox` (~1s) and `recover_jobs` (~5s),
  plus the daily retention prune (`jobs.prune_expired` — a no-op unless `RETENTION_DAYS` is set).

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
| `foreman_process_heartbeat_age_seconds{process}` | Seconds since the process last completed a work cycle (`+Inf` = never). | `process="listener"` > ~30s. | Push-dispatch is down; latency degrades to the Beat poll — see [the listener is dead](#the-listener-is-dead). |

These thresholds are not just documentation — they are committed as live
Prometheus rules; see [SLOs & alerts](#slos--alerts).

## SLOs & alerts

The committed source of truth for "what pages" is
[`ops/observability/prometheus/alerts.yml`](../ops/observability/prometheus/alerts.yml)
— alert rules with runbook-link annotations, plus recording rules for the SLO
signals. Run it locally with `make observe` (the full stack + Prometheus at
`http://localhost:9090` and Grafana at `http://localhost:3000`, dashboard and
datasource auto-provisioned, no login). A drift-guard test
(`src/jobs/tests/test_alert_rules.py`) fails CI if a rule references a metric that
`/metrics` no longer exports.

| SLO | Target | Signal (recording rule) |
|---|---|---|
| Processing latency | p95 < 5 s | `foreman:job_processing_seconds:p95` |
| Queue wait | p95 < 30 s | `foreman:job_queue_wait_seconds:p95` |
| Failure ratio | < 1% over 15 m | `foreman:jobs_error_ratio:15m` |
| Dead-letter growth | 0 per 15 m | `increase(foreman_jobs_processed_total{status="DEAD_LETTER"}[15m])` |

Alerts: `ForemanDeadLetterGrowth`, `ForemanOutboxDispatchLagging` (> 30 s for
2 m), `ForemanProcessingStuck` (> 300 s for 5 m), `ForemanRetryBacklogGrowing`,
`ForemanListenerDead` (heartbeat age > 30 s for 2 m, **warn** — latency-only,
Beat keeps delivery correct), and the two SLO-breach alerts. Counter-derived rules deliberately use
`increase()`/`rate()` rather than absolute totals: the counters are DB-derived
([ADR 0006](adr/0006-load-testing-metrics.md)), so growth-based expressions
stay correct if retention pruning ever shrinks the underlying rows.

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

### The listener is dead

`ForemanListenerDead` fires when the `listener` heartbeat goes stale (`+Inf` means it
never started). The heartbeat is written after each completed dispatch cycle —
*progress*, not liveness — so a wedged listener stops beating within one backstop
sweep even if its process survives. Nothing is lost while it is down: Beat's
durability fallback keeps dispatching, so the only impact is push-dispatch latency
degrading to the poll interval ([ADR 0007](adr/0007-listen-notify-dispatch.md)).

Restart the `listener` service (Railway: redeploy it; locally: `make listener`) and
confirm `foreman_process_heartbeat_age_seconds{process="listener"}` drops below the
sweep interval. If it misbehaves persistently, withdrawing it entirely is safe — see
[deploy.md](deploy.md).

### A worker crashed mid-job

No action needed — recovery is automatic. A crashed worker's job stays `PROCESSING` until
its lease expires (`JOB_LEASE_SECONDS`), then `recover_jobs` reclaims it to `PENDING` (or
`DEAD_LETTER` if attempts are spent). A slow-but-alive worker that resumes after being
reaped is fenced out by its stale `lease_token` and cannot corrupt the row.

This scenario is executable: `make chaos` SIGKILLs the worker mid-import on an isolated
compose stack and asserts every job still succeeds with no lost or duplicated rows — see
[tests/chaos/README.md](../tests/chaos/README.md). CI repeats it nightly (`chaos.yml`, non-blocking).

### Web fails to deploy after `terraform apply` (private-network DNS race)

Symptom: right after `terraform apply` brings the demo back on, the `web`
deployment goes `FAILED` while `worker`/`beat`/`listener`/`postgres`/`redis`
all report `SUCCESS`, and the domain serves Railway's edge 404
(`{"status":"error","code":404,"message":"Application not found"}`) rather
than anything from Django. The deploy log ends with the pre-deploy container
raising:

```text
psycopg.OperationalError: failed to resolve host 'postgres.railway.internal':
  [Errno -2] Name or service not known
```

Cause: a **DNS race, not a credentials or connectivity fault** — `Name or
service not known` is a resolver miss, so the host did not exist yet.
`terraform apply` creates all six services in the same second, and Railway
publishes `<service>.railway.internal` only once that service has a *running*
deployment. Web's pre-deploy fired ~28 s before Postgres finished deploying.
Only `web` dies because it is the only service with a pre-deploy command: a
one-shot that resolves the host immediately and fails the whole deploy on a
non-zero exit. Celery retries its broker connection and Django opens DB
connections lazily, so the rest of the fleet rides out the same window.

Fix (already deployed, but the manual recovery if it recurs): Postgres is
healthy by the time you look, so simply redeploy `web` — the dashboard's
**Redeploy** on the failed deployment, or `make deploy VERSION=<current>`.
No Terraform change and no re-`apply`; the image and env are already correct.

Prevention: web's pre-deploy is
[`migrate_when_ready`](../src/jobs/management/commands/migrate_when_ready.py),
which retries the connection for 90 s before migrating, so a cold `apply` now
heals itself. It stays a single command because Railway does not document
`preDeployCommand` as shell-interpreted — a `wait && migrate` pair could
silently skip the wait. Only `OperationalError` is retried, so a genuinely
broken migration still fails on the first attempt instead of after the
timeout.

### Local test run can't reach Postgres (stale db container)

Symptom: a host `pytest`/`make ci` run errors en masse (every `django_db` test)
with Django's *"unable to create a connection to the 'postgres' database"*
`RuntimeWarning` — escalated to an error by the strict `filterwarnings=error`
config — even though `docker ps` shows `foreman-db-1` up and healthy.

Check the port binding, not the health status:

```bash
docker port foreman-db-1   # empty output = the container isn't publishing 5432
```

Cause: `docker compose up` reuses an existing `db` container, and a stale one
(observed under OrbStack after a daemon restart) can come up healthy **without
its host port binding** — in-network services still reach it, but
`localhost:5432` from the host is refused. Fix:

```bash
docker compose up -d --force-recreate db
```

The `pgdata` volume is untouched, so no local data is lost. Related one-off:
immediately after the OrbStack daemon starts, the compose `migrate` one-shot
can fail with *"failed to resolve host 'db'"* (a DNS race) — just rerun
`make up`.

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
| `INGEST_MAX_ROWS` | 10000 | Row cap for every CSV source, inline payloads included. |
| `REMOTE_SOURCE_MAX_BYTES` | 1048576 | Streamed byte cap on an `https://` source fetch (Content-Length is never trusted). |
| `REMOTE_SOURCE_TIMEOUT_SECONDS` | 10 | Per-request timeout for `https://` fetches. |
| `RETENTION_DAYS` | 0 (disabled) | Prune terminal jobs (+ cascaded records/events) and aged DISPATCHED outbox rows after N days. |
| `RETENTION_BATCH_SIZE` | 1000 | Rows per delete batch while pruning. |
| `DJANGO_LOG_FORMAT` | `json` | Log output format: `json` or `console`. |
| `CHANNELS_REDIS_URL` | `REDIS_URL` | Redis for the WebSocket channel layer. |

With retention enabled, the DB-derived `foreman_jobs_processed_total` counters shrink at
the prune horizon — alert on `increase()`/`rate()`, never absolute totals (the documented
monotonicity boundary in [ADR 0006](adr/0006-load-testing-metrics.md)). Pruning logs one
`retention.pruned` event with the deleted counts.

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

The production platform is **Railway** — topology, provisioning, CD flow, smoke checks, and
rollback live in [deploy.md](deploy.md) (decision rationale:
[ADR 0005](adr/0005-deployment-platform.md)).
