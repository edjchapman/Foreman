"""The /metrics endpoint: DB-derived queue gauges in Prometheus text format."""

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
