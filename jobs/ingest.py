"""Property-CSV ingestion — the swappable processing seam behind `process_job`.

Resolves a job's source to CSV text (inline, bundled ``sample:`` fixture, or a
remote ``https://`` URL via `jobs.sources`), then parses + validates rows into
dicts ready for `PropertyRecord`. Keeping this isolated means the worker doesn't
care where the CSV came from; further schemes (e.g. ``s3://``) slot in here
without touching the task or the model.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"
REQUIRED_COLUMNS = ("external_id", "address_line1", "city", "postcode")


class IngestError(Exception):
    """A source could not be read or parsed."""


class UnsupportedSourceError(IngestError):
    """The source scheme is not supported yet (e.g. s3://)."""


def load_csv_text(payload: dict) -> str:
    """Resolve a job payload to raw CSV text.

    Inline ``payload["csv"]`` wins; otherwise ``payload["source"]`` is a
    ``sample:<name>`` bundled fixture or an ``https://`` URL (fetched with the
    SSRF/size/timeout guards in `jobs.sources`).
    """
    inline: str | None = payload.get("csv")
    if inline:
        return inline

    source = str(payload.get("source", ""))
    if source.startswith("sample:"):
        return _read_sample(source.removeprefix("sample:"))
    if source.startswith("https://"):
        # Imported here, not at module top: sources.py imports IngestError from
        # this module, so a top-level import would be circular.
        from .sources import fetch_remote_csv

        return fetch_remote_csv(source)
    raise UnsupportedSourceError(f"unsupported source: {source!r}")


def _read_sample(name: str) -> str:
    # Resolve under SAMPLE_DIR and reject any path that escapes it (traversal-safe).
    candidate = (SAMPLE_DIR / name).resolve()
    if SAMPLE_DIR not in candidate.parents or not candidate.is_file():
        raise IngestError(f"sample not found: {name!r}")
    return candidate.read_text()


def parse_rows(text: str) -> tuple[list[dict], list[dict]]:
    """Parse CSV text into (records, errors). Records are PropertyRecord field dicts.

    Caps the row count at ``INGEST_MAX_ROWS`` for every source — inline payloads
    and remote fetches alike — so no import can grow the job row, its stored
    ``errors`` list, or the bulk-insert unboundedly.
    """
    reader = csv.DictReader(io.StringIO(text))
    records: list[dict] = []
    errors: list[dict] = []
    for line_no, row in enumerate(reader, start=1):
        if line_no > settings.INGEST_MAX_ROWS:
            raise IngestError(f"CSV exceeds the {settings.INGEST_MAX_ROWS}-row limit")
        record, reason = _validate_row(row)
        if record is None:
            errors.append({"row": line_no, "reason": reason})
        else:
            records.append(record)
    return records, errors


def _validate_row(row: dict) -> tuple[dict | None, str | None]:
    missing = [c for c in REQUIRED_COLUMNS if not (row.get(c) or "").strip()]
    if missing:
        return None, f"missing required: {', '.join(missing)}"

    record = {c: row[c].strip() for c in REQUIRED_COLUMNS}
    try:
        record["price"] = _to_decimal(row.get("price"))
        record["bedrooms"] = _to_int(row.get("bedrooms"))
    except (InvalidOperation, ValueError) as exc:
        return None, f"invalid numeric value: {exc}"
    return record, None


def _to_decimal(value: str | None) -> Decimal | None:
    value = (value or "").strip()
    return Decimal(value) if value else None


def _to_int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None
