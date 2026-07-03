# Load testing

Turning the reliability *claims* into measured numbers. A [Locust](https://locust.io)
harness drives the real submit → outbox → worker pipeline; new Prometheus counters
and histograms make throughput and latency observable at `/metrics` while it runs.
Rationale: [ADR 0006](adr/0006-load-testing-metrics.md).

## Run it

The harness needs a live stack (web + Redis + Celery workers), so it is excluded
from `make ci` — like the [`e2e/`](../e2e) suite.

```bash
# 1. Bring up a stack and workers (three terminals, or a deployed platform):
make up          # web + Postgres + Redis
make worker      # Celery worker(s)
make beat        # outbox relay + recovery scheduler

# 2. Drive load (opens the Locust web UI at http://localhost:8089):
make load

# Headless against the deployed platform, fixed rate and duration:
FOREMAN_LOAD_URL=https://foreman-demo.up.railway.app \
  uv run --group load locust -f load/locustfile.py --headless -u 20 -r 5 -t 2m
```

`-u` users, `-r` ramp/second, `-t` duration. Scale load with these flags, not by
editing the locustfile. See [`load/README.md`](../load/README.md) for the knobs.

## Read the results

Scrape `/metrics` during and after the run (or point Prometheus/Grafana at it).
The series added for exactly this purpose:

| Question | PromQL |
|---|---|
| Throughput (jobs/s) | `rate(foreman_jobs_processed_total{status="SUCCEEDED"}[1m])` |
| Error ratio | `sum(rate(foreman_jobs_processed_total{status=~"FAILED\|DEAD_LETTER"}[5m])) / sum(rate(foreman_jobs_processed_total[5m]))` |
| p95 processing latency | `histogram_quantile(0.95, rate(foreman_job_processing_seconds_bucket[5m]))` |
| p95 queue wait | `histogram_quantile(0.95, rate(foreman_job_queue_wait_seconds_bucket[5m]))` |
| Relay backlog under load | `foreman_outbox_pending` |
| Dispatch lag | `foreman_outbox_oldest_pending_age_seconds` |
| Worker concurrency | `foreman_jobs{status="PROCESSING"}` |

Locust's own stats give the client-side view: submit latency (`POST /api/v1/jobs/`)
and the synthetic `submit → terminal` end-to-end lifecycle timing.

## Baseline

Captured **2026-07-03** against a **local host stack** — daphne web (single
process), Celery worker at `--concurrency 4`, Beat relay at the default 1s poll,
Postgres 16 + Redis — driven headless at **`-u 20 -r 5 -t 90s`**. Reproduce with
`make load` (or the headless command above) after `make up` + `make worker` +
`make beat`; numbers scale with hardware, worker concurrency, and the poll
interval, so treat them as a shape, not an SLA.

| Metric | Value |
|---|---|
| Sustained throughput | **~44 jobs/s** (3,958 processed in the window) |
| Submit latency p50 / p95 (POST) | 29 ms / 93 ms |
| Processing latency p50 / p95 | 28 ms / 77 ms |
| Queue wait p50 / p95 | 0.81 s / 2.1 s |
| End-to-end p50 / p95 (submit → terminal) | 1.1 s / 1.6 s |
| Peak outbox backlog | 63 events |
| Error ratio | 0% (0 of 3,958) |

The shape is the point. **Processing is fast (p95 77 ms) but queue wait dominates
(p95 2.1 s)** — under sustained load the 1s Beat poll and a bursty ~60-deep
backlog, not the work itself, set end-to-end latency. That localizes the next
optimization precisely: `LISTEN/NOTIFY` push dispatch (ADR 0001 /
[case study](case-study.md#what-id-build-next)) would collapse the queue-wait
tail without touching the write path. Worker concurrency held at 4 (fully
utilized) with zero failures and zero dead-letters.

> The numbers are secondary to the loop: **generate load → observe it through the
> counters and histograms → the reliability claims stop being assertions.**
