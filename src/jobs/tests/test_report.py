"""The report endpoint streams a succeeded job's records as a CSV attachment.

Consuming the body needs care: `stream_report` is an async generator, and the
suite runs with `filterwarnings=error`, so draining it synchronously
(`iter(resp)` / `resp.getvalue()`) would turn Django's sync-consumption Warning
into a failure. `_drain` enters via `async_to_sync` from the test's main thread;
asgiref's thread-sensitivity contract then runs the ORM work back ON that same
thread — same DB connection, so it is SQLite-safe (unlike the Postgres-only
`WebsocketCommunicator` tests, cf. test_consumers.py).
"""

import csv
import io
import uuid

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection

from jobs.models import Job, PropertyRecord

from .factories import JobFactory

pytestmark = pytest.mark.django_db


def _drain(resp):
    async def collect():
        return b"".join([chunk async for chunk in resp.streaming_content])

    return async_to_sync(collect)()


def _rows(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode())))


def _succeeded_job() -> Job:
    return JobFactory(status=Job.Status.SUCCEEDED)


def test_report_streams_imported_records_as_csv(api_client):
    from jobs.reports import REPORT_COLUMNS
    from jobs.tasks import dispatch_outbox

    resp = api_client.post(
        "/api/v1/jobs/",
        {"job_type": "property_csv_import", "payload": {"source": "sample:properties.csv"}},
        format="json",
    )
    dispatch_outbox()
    job = Job.objects.get(pk=resp.data["id"])
    assert job.status == Job.Status.SUCCEEDED

    resp = api_client.get(f"/api/v1/jobs/{job.id}/report/")

    assert resp.status_code == 200
    assert resp.streaming is True
    assert resp["Content-Type"] == "text/csv; charset=utf-8"
    assert resp["Content-Disposition"] == f'attachment; filename="foreman-report-{job.id}.csv"'
    rows = _rows(_drain(resp))
    assert rows[0] == list(REPORT_COLUMNS)
    assert len(rows) - 1 == job.result["rows_imported"]
    # DecimalField round-trips at scale 2 ("245000.00"), not the fixture's "245000".
    assert rows[1] == ["P-1001", "12 Acacia Avenue", "Manchester", "M14 5TP", "245000.00", "3"]


@pytest.mark.parametrize(
    "job_status",
    [Job.Status.PENDING, Job.Status.PROCESSING, Job.Status.FAILED, Job.Status.DEAD_LETTER],
)
def test_report_conflicts_until_succeeded(api_client, job_status):
    job = JobFactory(status=job_status)

    resp = api_client.get(f"/api/v1/jobs/{job.id}/report/")

    assert resp.status_code == 409
    assert resp.data["status"] == job_status


def test_report_unknown_job_is_404(api_client):
    resp = api_client.get(f"/api/v1/jobs/{uuid.uuid4()}/report/")

    assert resp.status_code == 404


def test_report_with_no_records_is_header_only(api_client):
    job = _succeeded_job()

    resp = api_client.get(f"/api/v1/jobs/{job.id}/report/")

    assert resp.status_code == 200
    assert _drain(resp) == b"external_id,address_line1,city,postcode,price,bedrooms\r\n"


def test_report_escapes_csv_metacharacters(api_client):
    job = _succeeded_job()
    PropertyRecord.objects.create(
        job=job,
        external_id="P-9001",
        address_line1='1 "The Firs", Elm Rd',
        city="York",
        postcode="YO1 7HH",
    )

    rows = _rows(_drain(api_client.get(f"/api/v1/jobs/{job.id}/report/")))

    assert rows[1] == ["P-9001", '1 "The Firs", Elm Rd', "York", "YO1 7HH", "", ""]


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="ASGI drain opens a second DB connection; in-memory SQLite can't share it",
)
async def test_report_streams_under_asgi(async_client):
    """Regression guard: the body must stream through Django's *async* path.

    If `stream_report` ever regresses to a sync iterator, ASGI consumption raises
    Django's buffering Warning — an error under this suite's `filterwarnings=error`.
    """
    job = await sync_to_async(_succeeded_job)()
    await PropertyRecord.objects.acreate(
        job=job, external_id="P-1", address_line1="1 A St", city="Hull", postcode="HU1 1AA"
    )

    resp = await async_client.get(f"/api/v1/jobs/{job.id}/report/")

    assert resp.status_code == 200
    body = b"".join([chunk async for chunk in resp.streaming_content])
    assert body.startswith(b"external_id,")
    assert b"P-1" in body
