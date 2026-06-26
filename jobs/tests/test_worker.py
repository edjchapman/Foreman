"""Tests for the worker (`jobs.tasks.process_job`) and CSV ingestion."""

import pytest

from jobs.models import Job, PropertyRecord
from jobs.tasks import process_job
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db

# 3 valid rows, 2 invalid (missing required field, non-numeric price).
MIXED_CSV = (
    "external_id,address_line1,city,postcode,price,bedrooms\n"
    "P-1,1 High St,Leeds,LS1 1AA,200000,2\n"
    "P-2,2 Low St,Leeds,LS1 1AB,not-a-number,3\n"
    ",3 No St,Leeds,LS1 1AC,150000,1\n"
    "P-4,4 Mid St,Leeds,LS1 1AD,175000,2\n"
    "P-5,5 End St,Leeds,LS1 1AE,,4\n"
)


def test_process_job_imports_sample_and_succeeds():
    job = JobFactory()  # payload → sample:properties.csv (5 rows)

    outcome = process_job(str(job.id))

    job.refresh_from_db()
    assert outcome == "succeeded"
    assert job.status == Job.Status.SUCCEEDED
    assert job.progress == 100
    assert job.attempts == 1
    assert job.result["rows_imported"] == 5
    assert job.result["rows_skipped"] == 0
    assert PropertyRecord.objects.filter(job=job).count() == 5


def test_process_job_counts_skipped_rows():
    job = JobFactory(payload={"csv": MIXED_CSV})

    process_job(str(job.id))

    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED
    assert job.result["rows_total"] == 5
    assert job.result["rows_imported"] == 3
    assert job.result["rows_skipped"] == 2
    assert len(job.result["errors"]) == 2
    assert PropertyRecord.objects.filter(job=job).count() == 3


def test_process_job_skips_non_pending_job():
    job = JobFactory(status=Job.Status.SUCCEEDED)

    outcome = process_job(str(job.id))

    job.refresh_from_db()
    assert outcome == "skipped"
    assert job.status == Job.Status.SUCCEEDED  # untouched
    assert PropertyRecord.objects.filter(job=job).count() == 0


def test_process_job_fails_on_unsupported_source():
    job = JobFactory(payload={"source": "s3://bucket/data.csv"})

    outcome = process_job(str(job.id))

    job.refresh_from_db()
    assert outcome == "failed"
    assert job.status == Job.Status.FAILED
    assert "unsupported source" in job.error
    assert PropertyRecord.objects.filter(job=job).count() == 0
