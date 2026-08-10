"""OpenTelemetry tracing setup — the single init seam (mirrors the Prometheus wiring).

Env-gated on ``settings.OTEL_ENABLED`` (default off), so CI, the test suite, and any
``OTEL_ENABLED=false`` deployment pay zero cost and see no behavioural change. When
enabled, each process (web / worker / beat / listener) builds a ``TracerProvider`` with a
distinct ``service.name`` and exports spans over OTLP/gRPC. Django, Celery, and psycopg
are auto-instrumented for the free spans; the two *manual* propagation seams that bridge
the transactional outbox — where auto-instrumentation cannot reach — live in
``jobs/services.py`` (inject at write) and ``jobs/tasks.py`` (re-attach at dispatch).

`configure_tracing` is called per-process from ``config/asgi.py`` (web) and Celery's
``worker_process_init`` / ``beat_init`` signals in ``config/celery.py`` — the latter so
each *forked* worker gets its own exporter thread (a ``BatchSpanProcessor`` thread does
not survive ``fork()``). See ADR 0008.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

TRACER_NAME = "foreman"

# Set-once per process. `set_tracer_provider` refuses a second provider, and after a
# `fork()` each worker child re-runs configure (its inherited flag is reset per child
# because the parent never configured — config happens only in the signal handlers).
_configured = False


def configure_tracing(service_name: str) -> None:
    """Install a `TracerProvider` for this process and auto-instrument the stack.

    No-op when ``OTEL_ENABLED`` is false or already configured — safe to call from
    every entrypoint. Endpoint and auth headers come from the standard
    ``OTEL_EXPORTER_OTLP_*`` env vars the OTLP exporter reads itself.
    """
    global _configured
    if _configured or not settings.OTEL_ENABLED:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": os.environ.get("OTEL_SERVICE_VERSION", "dev"),
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.OTEL_SAMPLER_RATIO)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # Auto-instrument the framework hops: HTTP server spans (Django), task
    # produce/consume spans + broker context propagation (Celery), and DB spans (psycopg).
    DjangoInstrumentor().instrument()
    CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]  # celery instrumentor ctor is untyped
    PsycopgInstrumentor().instrument()

    _configured = True


def get_tracer() -> trace.Tracer:
    """The app's tracer — a no-op tracer until `configure_tracing` runs."""
    return trace.get_tracer(TRACER_NAME)


def inject_trace() -> dict[str, str]:
    """Serialise the current trace context into a fresh W3C carrier dict.

    Returns ``{}`` when tracing is off or no span is active — safe to persist into
    ``OutboxEvent.payload`` unconditionally.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def span_from_carrier(name: str, carrier: dict | None) -> Iterator[trace.Span]:
    """Start ``name`` as a child of the trace context serialised in ``carrier``.

    This is the extraction half of the outbox bridge: the API's context, persisted in
    the outbox row, is re-hydrated here so the dispatched work links back to the original
    request. An empty/absent carrier simply starts a new root span.
    """
    parent = propagate.extract(carrier or {})
    with get_tracer().start_as_current_span(name, context=parent) as span:
        yield span
