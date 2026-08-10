# Load testing & event-rate / latency metrics

To measure the pipeline under pressure, a **Locust** harness (`make load`, own
dependency group, excluded from CI like `tests/e2e/`) drives submit → outbox → worker,
and `/metrics` gains rate and latency series that stay **DB-derived**: terminal
states are monotonic (a job never leaves one), so live row-counts *are*
cumulative totals (`foreman_jobs_processed_total{status}`), and queue-wait /
processing histograms are bucketed DB-side in one SQL `FILTER` query each over
the new `started_at`/`finished_at` timestamps — no multiprocess mode, no
Pushgateway, O(1) scrape cost.

## Considered Options

- **prometheus-client multiprocess mode / a Pushgateway** — machinery a
  three-container demo doesn't earn ([ADR 0003](0003-observability.md)'s
  reasoning, unchanged).
- **k6** — a separate Go binary outside the `uv`-managed, Python-only toolchain.

## Consequences

- Resolves ADR 0003's gauges-only tradeoff: `rate()` gives throughput and error
  ratio, `histogram_quantile` gives p95 latency.
- Counter monotonicity has a documented boundary: redrive and retention pruning
  can *decrease* a total; Prometheus `rate()` tolerates the reset.
- The load test's headline finding — latency dominated by queue wait, not
  processing — motivated [ADR 0007](0007-listen-notify-dispatch.md). Results:
  [docs/load-testing.md](../load-testing.md).
