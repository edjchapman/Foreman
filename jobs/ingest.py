"""Property-CSV ingestion — the swappable processing seam behind `process_job`.

Resolves a job's source to CSV text, then parses + validates rows into dicts
ready for `PropertyRecord`. Keeping this isolated means the worker doesn't care
where the CSV came from, and M3+ can add real object-store/HTTP sources here
without touching the task or the model.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"
REQUIRED_COLUMNS = ("external_id", "address_line1", "city", "postcode")


class IngestError(Exception):
    """A source could not be read or parsed."""


class UnsupportedSourceError(IngestError):
    """The source scheme is not supported yet (e.g. s3://, https://)."""


def load_csv_text(payload: dict) -> str:
    """Resolve a job payload to raw CSV text.

    Inline ``payload["csv"]`` wins; otherwise ``payload["source"]`` must be a
    ``sample:<name>`` reference to a bundled fixture. Remote schemes are M3+.
    """
    inline: str | None = payload.get("csv")
    if inline:
        return inline

    source = str(payload.get("source", ""))
    if source.startswith("sample:"):
        return _read_sample(source.removeprefix("sample:"))
    raise UnsupportedSourceError(f"unsupported source: {source!r}")


def _read_sample(name: str) -> str:
    # Resolve under SAMPLE_DIR and reject any path that escapes it (traversal-safe).
    candidate = (SAMPLE_DIR / name).resolve()
    if SAMPLE_DIR not in candidate.parents or not candidate.is_file():
        raise IngestError(f"sample not found: {name!r}")
    return candidate.read_text()


def parse_rows(text: str) -> tuple[list[dict], list[dict]]:
    """Parse CSV text into (records, errors). Records are PropertyRecord field dicts."""
    reader = csv.DictReader(io.StringIO(text))
    records: list[dict] = []
    errors: list[dict] = []
    for line_no, row in enumerate(reader, start=1):
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
