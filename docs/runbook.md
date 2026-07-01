# Runbook

Operating guide for Foreman's job pipeline: what the endpoints and metrics mean, how to
read the logs, and what to do when something goes wrong. The *why* lives in the
[ADRs](adr/README.md); this is the operational how-to.

## Services

- **web** — the DRF API; also serves `/healthz`, `/readyz`, `/metrics`.
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
`job.dead_letter`, plus `recover.requeued` / `recover.reaped` from the recovery scan.

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
