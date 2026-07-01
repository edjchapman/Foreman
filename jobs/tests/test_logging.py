"""Structured-logging tests: the JSON formatter and the task event stream.

The formatter is tested directly because pytest's `caplog` installs its own
handler, so running tasks never exercises `config.logformat.JsonFormatter` — the
direct tests are what keep that module covered.
"""

import json
import logging
import sys

import pytest
from django.conf import settings

from config.logformat import JsonFormatter
from jobs import tasks
from jobs.tasks import process_job
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db


def _raise_transient(job):
    raise RuntimeError("boom")


def _record(msg, **extra):
    """Build a LogRecord, promoting `extra` kwargs to attributes as logging's `extra=` does."""
    record = logging.LogRecord(
        name="jobs.tasks",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --- The formatter ----------------------------------------------------------------


def test_json_formatter_emits_core_schema():
    payload = json.loads(JsonFormatter().format(_record("job.succeeded")))
    assert payload["event"] == "job.succeeded"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "jobs.tasks"
    assert "timestamp" in payload


def test_json_formatter_serializes_extra_fields():
    payload = json.loads(JsonFormatter().format(_record("job.claimed", job_id="abc", attempts=2)))
    assert payload["job_id"] == "abc"
    assert payload["attempts"] == 2


def test_json_formatter_includes_exception_text():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = _record("job.failed", exc_info=sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "RuntimeError: boom" in payload["error"]


# --- The task event stream --------------------------------------------------------


def test_process_job_emits_claimed_and_succeeded(caplog):
    job = JobFactory()
    with caplog.at_level(logging.INFO, logger="jobs.tasks"):
        assert process_job(str(job.id)) == "succeeded"

    events = [r.getMessage() for r in caplog.records]
    assert "job.claimed" in events
    succeeded = next(r for r in caplog.records if r.getMessage() == "job.succeeded")
    assert str(succeeded.job_id) == str(job.id)
    assert succeeded.attempts == 1
    assert succeeded.latency_ms >= 0
    assert succeeded.rows_imported == 5


def test_retry_then_dead_letter_events(monkeypatch, caplog):
    monkeypatch.setattr(tasks, "_import_properties", _raise_transient)
    job = JobFactory()
    with caplog.at_level(logging.INFO, logger="jobs.tasks"):
        for _ in range(settings.JOB_MAX_ATTEMPTS):
            process_job(str(job.id))

    events = [r.getMessage() for r in caplog.records]
    assert "job.retry_scheduled" in events
    retry = next(r for r in caplog.records if r.getMessage() == "job.retry_scheduled")
    assert retry.retry_in_s >= 0
    dead = next(r for r in caplog.records if r.getMessage() == "job.dead_letter")
    assert dead.levelname == "WARNING"
    assert dead.error_class == "RuntimeError"


def test_permanent_failure_event(caplog):
    job = JobFactory(payload={"source": "s3://bucket/data.csv"})
    with caplog.at_level(logging.INFO, logger="jobs.tasks"):
        assert process_job(str(job.id)) == "failed"

    failed = next(r for r in caplog.records if r.getMessage() == "job.failed")
    assert failed.error_class == "UnsupportedSourceError"
    assert "unsupported source" in failed.error
