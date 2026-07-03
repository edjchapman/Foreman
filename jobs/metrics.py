"""Prometheus metrics — queue golden-signals derived from the DB at scrape time.

We expose metrics computed live from Postgres rather than process-local counters.
The Celery worker and Beat run in separate *containers* from the web server that
serves `/metrics`, so a counter incremented in the worker would be invisible here
without prometheus multiprocess mode (which shares a directory — no help across
containers). Deriving from the database is cross-process-true with zero extra
machinery, and it extends cleanly past gauges:

- **Gauges** — jobs-by-status, outbox backlog and age, retry-scheduled depth,
  oldest in-flight age. Point-in-time queue depths.
- **Counter** — `foreman_jobs_processed_total{status}`. Terminal states are
  never left (barring `redrive` / retention pruning), so the current count of
  rows in a terminal state equals the cumulative number that ever reached it —
  a monotonic, `rate()`-able total sourced from a plain point-in-time query.
- **Histograms** — queue-wait and processing latency, bucketed DB-side in one
  query each (SQL `FILTER`), so scrape cost stays O(1) queries as the table grows.

A dedicated registry (not the global default) keeps the endpoint to these domain
metrics — no `python_gc_*` noise — and keeps the collector re-import-safe.
See ADR 0003 (observability) and ADR 0006 (load testing & event-rate metrics).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

from django.db.models import (
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Min,
    Q,
    Sum,
)
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from prometheus_client.registry import Collector

from .models import Job, OutboxEvent

# Jobs never leave these states (barring redrive), so a live count of rows in
# them doubles as a cumulative "how many ever reached this outcome" counter.
TERMINAL_STATUSES = (
    Job.Status.SUCCEEDED.value,
    Job.Status.FAILED.value,
    Job.Status.DEAD_LETTER.value,
)

# Histogram bucket upper bounds in seconds; "+Inf" is appended at emit time.
LATENCY_BUCKETS_SECONDS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


class ForemanCollector(Collector):
    """Yield job/outbox metrics, querying the database at scrape time."""

    def collect(self) -> Iterator[GaugeMetricFamily | CounterMetricFamily | HistogramMetricFamily]:
        now = timezone.now()
        yield self._jobs_by_status()
        yield self._jobs_processed_total()
        yield self._outbox_pending()
        yield self._outbox_oldest_age(now)
        yield self._retry_scheduled(now)
        yield self._processing_oldest_age(now)
        yield self._queue_wait_histogram()
        yield self._processing_histogram()

    def _jobs_by_status(self) -> GaugeMetricFamily:
        gauge = GaugeMetricFamily(
            "foreman_jobs", "Number of jobs currently in each status.", labels=["status"]
        )
        counts = {
            row["status"]: row["n"] for row in Job.objects.values("status").annotate(n=Count("id"))
        }
        # Zero-fill absent statuses so DLQ depth (status="DEAD_LETTER") always reports.
        for status in Job.Status.values:
            gauge.add_metric([status], counts.get(status, 0))
        return gauge

    def _jobs_processed_total(self) -> CounterMetricFamily:
        counter = CounterMetricFamily(
            "foreman_jobs_processed",
            "Cumulative jobs that have reached each terminal state.",
            labels=["status"],
        )
        counts = {
            row["status"]: row["n"]
            for row in Job.objects.filter(status__in=TERMINAL_STATUSES)
            .values("status")
            .annotate(n=Count("id"))
        }
        # Zero-fill so an error-rate query has a series to divide even before any failure.
        for status in TERMINAL_STATUSES:
            counter.add_metric([status], float(counts.get(status, 0)))
        return counter

    def _outbox_pending(self) -> GaugeMetricFamily:
        pending = OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).count()
        return GaugeMetricFamily(
            "foreman_outbox_pending", "Undispatched outbox events (relay backlog).", value=pending
        )

    def _outbox_oldest_age(self, now: datetime) -> GaugeMetricFamily:
        oldest = OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).aggregate(
            oldest=Min("created_at")
        )["oldest"]
        age = (now - oldest).total_seconds() if oldest else 0.0
        return GaugeMetricFamily(
            "foreman_outbox_oldest_pending_age_seconds",
            "Age of the oldest undispatched outbox event (dispatch lag).",
            value=age,
        )

    def _retry_scheduled(self, now: datetime) -> GaugeMetricFamily:
        waiting = Job.objects.filter(
            status=Job.Status.PENDING, available_at__isnull=False, available_at__gt=now
        ).count()
        return GaugeMetricFamily(
            "foreman_jobs_retry_scheduled",
            "PENDING jobs waiting on backoff (retry queue depth).",
            value=waiting,
        )

    def _processing_oldest_age(self, now: datetime) -> GaugeMetricFamily:
        oldest = Job.objects.filter(status=Job.Status.PROCESSING).aggregate(
            oldest=Min("updated_at")
        )["oldest"]
        age = (now - oldest).total_seconds() if oldest else 0.0
        return GaugeMetricFamily(
            "foreman_jobs_processing_oldest_age_seconds",
            "Age of the oldest in-flight job (stuck-lease / worker-death signal).",
            value=age,
        )

    def _queue_wait_histogram(self) -> HistogramMetricFamily:
        return self._duration_histogram(
            "foreman_job_queue_wait_seconds",
            "Time from job submission to first worker claim.",
            start="created_at",
            end="started_at",
        )

    def _processing_histogram(self) -> HistogramMetricFamily:
        return self._duration_histogram(
            "foreman_job_processing_seconds",
            "Time from claim to terminal state (final attempt).",
            start="started_at",
            end="finished_at",
        )

    def _duration_histogram(
        self, name: str, documentation: str, *, start: str, end: str
    ) -> HistogramMetricFamily:
        """Bucket (end - start) durations DB-side: one query, cumulative counts."""
        duration = ExpressionWrapper(F(end) - F(start), output_field=DurationField())
        rows = Job.objects.filter(**{f"{start}__isnull": False, f"{end}__isnull": False})
        # SQL FILTER per bound: each counts durations <= bound, so results are already
        # cumulative — exactly the Prometheus histogram wire format.
        aggregates: dict = {
            f"le_{i}": Count("id", filter=Q(_d__lte=timedelta(seconds=bound)))
            for i, bound in enumerate(LATENCY_BUCKETS_SECONDS)
        }
        aggregates["total"] = Count("id")
        aggregates["total_duration"] = Sum("_d")
        agg = rows.annotate(_d=duration).aggregate(**aggregates)

        buckets: list[tuple[str, float]] = [
            (str(bound), float(agg[f"le_{i}"])) for i, bound in enumerate(LATENCY_BUCKETS_SECONDS)
        ]
        buckets.append(("+Inf", float(agg["total"])))
        total_duration = agg["total_duration"]
        sum_seconds = total_duration.total_seconds() if total_duration else 0.0

        histogram = HistogramMetricFamily(name, documentation)
        histogram.add_metric([], buckets, sum_value=sum_seconds)
        return histogram


REGISTRY = CollectorRegistry()
REGISTRY.register(ForemanCollector())


def metrics_view(request: HttpRequest) -> HttpResponse:
    """Expose the domain metrics in Prometheus text exposition format."""
    return HttpResponse(generate_latest(REGISTRY), content_type=CONTENT_TYPE_LATEST)
