"""Demo-only fault injection — makes the reliability machinery *visible*.

The worker's retry/backoff/dead-letter/redrive path is the most interesting part
of the system, but the ordinary happy-path import never exercises it. These
synthetic sources let the demo page drive a job through those states on purpose:

- ``fault:flaky`` — fails the first attempts, then succeeds on the last one the
  retry budget allows. Demonstrates **retry + backoff + automatic recovery**
  (ends SUCCEEDED, no operator needed).
- ``fault:heal-after:<seconds>`` — fails until ``<seconds>`` after the job was
  created, then succeeds. Sized so the automatic retries exhaust into
  **DEAD_LETTER** first; a later operator ``redrive`` (once the window has
  passed) then heals it. Models a transient downstream outage that resolves
  before the operator retries.

Both raise a plain :class:`RuntimeError` — a *non*-``IngestError`` — so they flow
into the worker's *transient* branch (retry, then dead-letter), not the poison
``FAILED`` branch. On the heal path they resolve to the bundled sample CSV so the
job actually imports rows and the report works. This module is intentionally the
only place that knows about ``fault:`` sources; nothing else needs to change.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .ingest import load_csv_text

FAULT_PREFIX = "fault:"
HEAL_AFTER_PREFIX = "fault:heal-after:"

# The CSV a healed fault job imports, so success is indistinguishable from a real import.
_HEALED_PAYLOAD = {"source": "sample:properties.csv"}


class InjectedFaultError(RuntimeError):
    """A simulated transient failure — deliberately not an IngestError, so it retries."""


def is_fault_source(source: str) -> bool:
    """True if ``source`` is a demo fault-injection reference."""
    return source.startswith(FAULT_PREFIX)


def load_fault_csv(source: str, *, attempts: int, created_at: datetime) -> str:
    """Resolve a ``fault:`` source: raise while the fault is 'active', else the sample CSV.

    ``attempts`` is the current attempt number (>=1, already incremented at claim);
    ``created_at`` is the job's creation time (survives ``redrive``, so a
    ``heal-after`` job heals on a later redrive even though attempts reset).
    """
    if source == "fault:flaky":
        # Fail every attempt but the last the budget allows, so automatic retries recover it.
        if attempts < settings.JOB_MAX_ATTEMPTS:
            raise InjectedFaultError(f"simulated transient failure (attempt {attempts})")
        return load_csv_text(_HEALED_PAYLOAD)

    if source.startswith(HEAL_AFTER_PREFIX):
        window = _parse_window(source)
        if timezone.now() < created_at + timedelta(seconds=window):
            raise InjectedFaultError(f"simulated outage — heals {window:g}s after submission")
        return load_csv_text(_HEALED_PAYLOAD)

    raise InjectedFaultError(f"unknown fault source: {source!r}")


def _parse_window(source: str) -> float:
    raw = source.removeprefix(HEAL_AFTER_PREFIX)
    try:
        return float(raw)
    except ValueError as exc:
        raise InjectedFaultError(f"invalid heal-after window: {raw!r}") from exc
