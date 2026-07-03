# Load harness

Locust load test that drives the real submit → outbox → worker pipeline, so the
reliability claims (throughput, backpressure, latency under concurrency) become
*measured* rather than argued. See [`docs/load-testing.md`](../docs/load-testing.md)
for how to read the results and a captured baseline; the rationale is
[ADR 0006](../docs/adr/0006-load-testing-metrics.md).

This directory mirrors the [`e2e/`](../e2e) suite: a separate dependency group
(`load`), its own `make` target, and **excluded from `make ci`** because it needs
a live platform (web + Redis + Celery workers), not just a database.

## Run it

```bash
# Against a locally running stack (make up + make worker + make beat):
make load                                            # opens the Locust web UI at :8089

# Headless, retargeting the deployed platform at a fixed rate for a fixed time:
FOREMAN_LOAD_URL=https://foreman-demo.up.railway.app \
  uv run --group load locust -f load/locustfile.py \
  --headless -u 20 -r 5 -t 2m
```

- `FOREMAN_LOAD_URL` — target base URL (default `http://localhost:8000`).
- `-u` users, `-r` ramp/s, `-t` duration; scale load here, not in the locustfile.
- `FOREMAN_LOAD_LIFECYCLE_TIMEOUT` — seconds the `submit → terminal` task waits
  for a job to finish before recording a timeout (default `60`).

## What to watch

While it runs, scrape `/metrics` (or point Prometheus/Grafana at it) and watch the
counters and histograms added for exactly this purpose — throughput, p95 latency,
and outbox backlog. The PromQL is in [`docs/load-testing.md`](../docs/load-testing.md).
