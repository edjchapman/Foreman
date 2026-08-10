"""End-to-end M2 pipeline: submit → outbox → relay → worker → SUCCEEDED.

Under the autouse eager-Celery fixture, `dispatch_outbox` runs `process_job`
inline, so a single relay call drives the whole pipeline in-process. This proves
the wiring; cross-process contention (SKIP LOCKED) is a Postgres-runtime property
exercised in CI, not modelled here.
"""

import pytest

from jobs.models import Job, OutboxEvent, PropertyRecord

pytestmark = pytest.mark.django_db


def test_submit_then_dispatch_processes_job(api_client):
    from jobs.tasks import dispatch_outbox

    resp = api_client.post(
        "/api/v1/jobs/",
        {"job_type": "property_csv_import", "payload": {"source": "sample:properties.csv"}},
        format="json",
    )
    assert resp.status_code == 202
    job_id = resp.data["id"]
    assert resp.data["status"] == Job.Status.PENDING
    assert OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).count() == 1

    dispatched = dispatch_outbox()

    assert dispatched == 1
    job = Job.objects.get(pk=job_id)
    assert job.status == Job.Status.SUCCEEDED
    assert job.progress == 100
    assert job.result["rows_imported"] == 5
    assert PropertyRecord.objects.filter(job=job).count() == 5
    assert OutboxEvent.objects.filter(status=OutboxEvent.Status.DISPATCHED).count() == 1
