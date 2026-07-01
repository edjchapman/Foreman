"""Prometheus metrics — queue golden-signals derived from the DB at scrape time.

We expose *gauges* computed live from Postgres (jobs-by-status, outbox backlog and
age, retry-scheduled depth, oldest in-flight age) rather than process-local counters.
The Celery worker and Beat run in separate processes from the web server that serves
`/metrics`, so a counter incremented in the worker would be invisible here without
prometheus multiprocess mode; DB-derived gauges reflect true cross-process state and
sidestep that entirely. Event-rate counters (for `rate()`) are deferred — see ADR 0003.

A dedicated registry (not the global default) keeps the endpoint to these domain
metrics — no `python_gc_*` noise — and keeps the collector re-import-safe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from django.db.models import Count, Min
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from .models import Job, OutboxEvent


class ForemanCollector(Collector):
    """Yield job/outbox gauges, querying the database once per scrape."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        now = timezone.now()
        yield self._jobs_by_status()
        yield self._outbox_pending()
        yield self._outbox_oldest_age(now)
        yield self._retry_scheduled(now)
        yield self._processing_oldest_age(now)

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


REGISTRY = CollectorRegistry()
REGISTRY.register(ForemanCollector())


def metrics_view(request: HttpRequest) -> HttpResponse:
    """Expose the domain gauges in Prometheus text exposition format."""
    return HttpResponse(generate_latest(REGISTRY), content_type=CONTENT_TYPE_LATEST)
