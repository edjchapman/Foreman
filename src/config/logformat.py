"""Structured JSON log formatter — zero-dependency, one JSON object per line.

Emits a stable core schema (`timestamp`, `level`, `logger`, `event`) plus any
per-call fields passed via the stdlib `extra=` kwarg, so a log line reads as a
queryable event rather than a sentence (e.g. `{"event": "job.succeeded",
"job_id": "…", "attempts": 1, "latency_ms": 42}`). `event` is the log message,
used as the event *name*; keep it a short dotted token, not prose.

We diff each record against a reference `LogRecord`'s attributes to recover the
`extra=` fields, so callers add domain context without this formatter knowing
about it. Chosen over structlog: the log surface is small and this stays a
small formatter (see ADR 0003).

When a span is active, the current `trace_id`/`span_id` are promoted onto the line so
logs and traces are mutually navigable — one query pivots from a job's log event to its
distributed trace and back (see ADR 0008). Costs nothing when tracing is off: the
OpenTelemetry API returns an invalid span context and the keys are simply omitted.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

# Attributes present on every LogRecord; anything else in a record's __dict__ was
# supplied via `extra=` and is promoted to a top-level JSON field. Computed from a
# throwaway record so it tracks the running Python version rather than a hand-list.
_RESERVED: frozenset[str] = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a `LogRecord` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(
            {key: value for key, value in record.__dict__.items() if key not in _RESERVED}
        )
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            # W3C hex widths: 32 for the trace id, 16 for the span id.
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        # default=str keeps non-JSON values (UUID, Decimal, datetime) serialisable.
        return json.dumps(payload, default=str)
