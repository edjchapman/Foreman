"""Tests for CSV source resolution and parsing (`jobs.ingest`)."""

import pytest

from jobs.ingest import (
    IngestError,
    UnsupportedSourceError,
    load_csv_text,
    parse_rows,
)


def test_inline_csv_takes_precedence():
    text = load_csv_text({"csv": "external_id,address_line1,city,postcode\nP-1,1 St,Leeds,LS1"})
    assert text.startswith("external_id")


def test_sample_source_reads_bundled_fixture():
    text = load_csv_text({"source": "sample:properties.csv"})
    assert "P-1001" in text


def test_missing_sample_raises():
    with pytest.raises(IngestError):
        load_csv_text({"source": "sample:does-not-exist.csv"})


def test_path_traversal_is_rejected():
    with pytest.raises(IngestError):
        load_csv_text({"source": "sample:../settings.py"})


def test_unsupported_scheme_raises():
    with pytest.raises(UnsupportedSourceError):
        load_csv_text({"source": "s3://bucket/data.csv"})


def test_row_cap_rejects_an_oversized_csv(settings):
    settings.INGEST_MAX_ROWS = 2
    text = "external_id,address_line1,city,postcode\n" + "\n".join(
        f"P-{i},1 St,Leeds,LS1" for i in range(3)
    )

    with pytest.raises(IngestError, match="row limit"):
        parse_rows(text)


def test_row_cap_allows_exactly_the_limit(settings):
    settings.INGEST_MAX_ROWS = 3
    text = "external_id,address_line1,city,postcode\n" + "\n".join(
        f"P-{i},1 St,Leeds,LS1" for i in range(3)
    )

    records, errors = parse_rows(text)
    assert len(records) == 3
    assert errors == []


def test_parse_rows_separates_valid_and_invalid():
    text = (
        "external_id,address_line1,city,postcode,price,bedrooms\n"
        "P-1,1 St,Leeds,LS1 1AA,200000,2\n"
        "P-2,2 St,Leeds,LS1 1AB,oops,3\n"
        ",3 St,Leeds,LS1 1AC,150000,1\n"
    )
    records, errors = parse_rows(text)
    assert len(records) == 1
    assert records[0]["external_id"] == "P-1"
    assert {e["row"] for e in errors} == {2, 3}
