"""CSV report generation for succeeded jobs.

The generator is async on purpose: daphne serves this project (ADR 0004), and
under ASGI a *sync* iterator makes `StreamingHttpResponse` emit a Warning and
buffer the entire body (`sync_to_async(list)` in `HttpResponseBase.__aiter__`)
— the opposite of streaming. `aiterator()` keeps memory flat regardless of
import size.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Sequence

from .models import Job, PropertyRecord

REPORT_COLUMNS = ("external_id", "address_line1", "city", "postcode", "price", "bedrooms")
_CHUNK_SIZE = 500


def report_filename(job: Job) -> str:
    return f"foreman-report-{job.id}.csv"


def _csv_line(values: Sequence[str]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerow(values)
    return buf.getvalue().encode()


def _record_values(record: PropertyRecord) -> tuple[str, ...]:
    return (
        record.external_id,
        record.address_line1,
        record.city,
        record.postcode,
        "" if record.price is None else str(record.price),
        "" if record.bedrooms is None else str(record.bedrooms),
    )


async def stream_report(job: Job) -> AsyncIterator[bytes]:
    """Yield the job's imported records as CSV lines, header first."""
    yield _csv_line(REPORT_COLUMNS)
    queryset = PropertyRecord.objects.filter(job=job).order_by("id")
    async for record in queryset.aiterator(chunk_size=_CHUNK_SIZE):
        yield _csv_line(_record_values(record))
