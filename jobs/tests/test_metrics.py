"""The /metrics endpoint: DB-derived queue gauges, terminal-outcome counters, and
latency histograms in Prometheus text format."""

from datetime import timedelta

import pytest
from django.utils import timezone

from jobs.models import Job, OutboxEvent
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db


def _body(api_client):
    resp = api_client.get("/metrics")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    return resp.content.decode()


def _sample(body, name):
    for line in body.splitlines():
        if line.startswith(name + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{name} not found in metrics output")


def test_metrics_endpoint_exposes_gauges(api_client):
    body = _body(api_client)
    assert "# HELP foreman_jobs " in body
    assert "# TYPE foreman_jobs gauge" in body
    # Empty DB: every status zero-filled, and both age gauges take the 0.0 fallback.
    assert 'foreman_jobs{status="PENDING"} 0.0' in body
    assert "foreman_outbox_pending 0.0" in body
    assert "foreman_outbox_oldest_pending_age_seconds 0.0" in body
    assert "foreman_jobs_processing_oldest_age_seconds 0.0" in body


def test_jobs_gauge_reflects_status_counts(api_client):
    JobFactory.create_batch(2)  # PENDING (factory default)
    JobFactory(status=Job.Status.SUCCEEDED)
    JobFactory(status=Job.Status.DEAD_LETTER)

    body = _body(api_client)
    assert 'foreman_jobs{status="PENDING"} 2.0' in body
    assert 'foreman_jobs{status="SUCCEEDED"} 1.0' in body
    assert 'foreman_jobs{status="DEAD_LETTER"} 1.0' in body
    assert 'foreman_jobs{status="FAILED"} 0.0' in body  # zero-filled, no such jobs


def test_age_gauges_report_positive_when_rows_present(api_client):
    job = JobFactory(status=Job.Status.PROCESSING)
    Job.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(seconds=30))
    event = OutboxEvent.objects.create(job=job)
    OutboxEvent.objects.filter(pk=event.pk).update(
        created_at=timezone.now() - timedelta(seconds=30)
    )

    body = _body(api_client)
    assert "foreman_outbox_pending 1.0" in body
    # A positive age (vs the 0.0 fallback) proves the oldest-row branch ran.
    assert _sample(body, "foreman_outbox_oldest_pending_age_seconds") > 0
    assert _sample(body, "foreman_jobs_processing_oldest_age_seconds") > 0


def test_retry_scheduled_gauge(api_client):
    JobFactory(status=Job.Status.PENDING, available_at=timezone.now() + timedelta(hours=1))

    assert "foreman_jobs_retry_scheduled 1.0" in _body(api_client)


def test_processed_counter_counts_only_terminal_states(api_client):
    JobFactory.create_batch(2, status=Job.Status.SUCCEEDED)
    JobFactory(status=Job.Status.FAILED)
    JobFactory(status=Job.Status.DEAD_LETTER)
    JobFactory(status=Job.Status.PENDING)  # non-terminal: must not appear
    JobFactory(status=Job.Status.PROCESSING)  # non-terminal: must not appear

    body = _body(api_client)
    assert "# TYPE foreman_jobs_processed_total counter" in body
    assert 'foreman_jobs_processed_total{status="SUCCEEDED"} 2.0' in body
    assert 'foreman_jobs_processed_total{status="FAILED"} 1.0' in body
    assert 'foreman_jobs_processed_total{status="DEAD_LETTER"} 1.0' in body
    # A counter must be monotonic, so transient states are excluded entirely.
    assert 'foreman_jobs_processed_total{status="PENDING"}' not in body
    assert 'foreman_jobs_processed_total{status="PROCESSING"}' not in body


def test_processed_counter_zero_fills_terminal_states(api_client):
    body = _body(api_client)
    # Empty DB: every terminal series still reports, so an error-rate query has a
    # denominator before the first failure ever occurs.
    assert 'foreman_jobs_processed_total{status="SUCCEEDED"} 0.0' in body
    assert 'foreman_jobs_processed_total{status="FAILED"} 0.0' in body
    assert 'foreman_jobs_processed_total{status="DEAD_LETTER"} 0.0' in body


def _timed_job(*, queue_wait_s: float, processing_s: float):
    """A terminal job with backdated timestamps for a known queue-wait/processing split."""
    now = timezone.now()
    job = JobFactory(status=Job.Status.SUCCEEDED)
    # .update() bypasses auto_now_add on created_at so the durations are exact.
    Job.objects.filter(pk=job.pk).update(
        created_at=now - timedelta(seconds=queue_wait_s + processing_s),
        started_at=now - timedelta(seconds=processing_s),
        finished_at=now,
    )
    return job


def test_latency_histograms_bucket_durations(api_client):
    _timed_job(queue_wait_s=2.0, processing_s=0.3)

    body = _body(api_client)
    assert "# TYPE foreman_job_processing_seconds histogram" in body
    assert "# TYPE foreman_job_queue_wait_seconds histogram" in body

    # Processing 0.3s → first bucket it fits is le="0.5"; le="0.25" stays empty.
    assert _sample(body, 'foreman_job_processing_seconds_bucket{le="0.25"}') == 0.0
    assert _sample(body, 'foreman_job_processing_seconds_bucket{le="0.5"}') == 1.0
    assert _sample(body, 'foreman_job_processing_seconds_bucket{le="+Inf"}') == 1.0
    assert _sample(body, "foreman_job_processing_seconds_count") == 1.0
    assert _sample(body, "foreman_job_processing_seconds_sum") == pytest.approx(0.3, abs=0.05)

    # Queue-wait 2s → lands in le="2.5", not le="1.0".
    assert _sample(body, 'foreman_job_queue_wait_seconds_bucket{le="1.0"}') == 0.0
    assert _sample(body, 'foreman_job_queue_wait_seconds_bucket{le="2.5"}') == 1.0
    assert _sample(body, "foreman_job_queue_wait_seconds_sum") == pytest.approx(2.0, abs=0.05)


def test_histogram_buckets_are_cumulative_and_monotonic(api_client):
    # Two processing durations (0.3s, 3.5s) so buckets must step up, never down.
    _timed_job(queue_wait_s=1.0, processing_s=0.3)
    _timed_job(queue_wait_s=1.0, processing_s=3.5)

    body = _body(api_client)
    bounds = ("0.05", "0.1", "0.25", "0.5", "1.0", "2.5", "5.0", "10.0", "30.0", "60.0", "+Inf")
    counts = [_sample(body, f'foreman_job_processing_seconds_bucket{{le="{b}"}}') for b in bounds]
    assert counts == sorted(counts)  # cumulative ⇒ non-decreasing
    assert counts[-1] == 2.0  # +Inf holds every observation
    # 0.3s ≤ 0.5 but 3.5s > 0.5, so exactly one observation sits at le="0.5".
    assert _sample(body, 'foreman_job_processing_seconds_bucket{le="0.5"}') == 1.0
