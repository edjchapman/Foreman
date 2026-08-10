"""Distributed-tracing tests: propagation across the outbox boundary + the init seam.

The pipeline deliberately severs the in-process call chain at the transactional outbox —
the API commits an `OutboxEvent` row and returns; a separate relay dispatches it later —
so OpenTelemetry's auto-instrumentation cannot connect the trace on its own.
`jobs.services.submit_job` persists the trace context into the outbox row and
`jobs.tasks.dispatch_outbox` re-hydrates it. These tests prove that bridge keeps one
`trace_id` across the gap — the novel part of the design. See ADR 0008.

Tracing is off in the rest of the suite (OTEL_ENABLED defaults false); here the `spans`
fixture opts in with an in-memory exporter, mirroring the autouse InMemory channel layer.
"""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from config import otel
from jobs.services import submit_job
from jobs.tasks import dispatch_outbox

pytestmark = pytest.mark.django_db

# One exporter reused across tests: `set_tracer_provider` is set-once per process, so the
# fixture installs the SDK provider the first time and thereafter only clears the exporter.
_EXPORTER = InMemorySpanExporter()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def test_trace_propagates_across_the_outbox(spans):
    """A job's trace survives the outbox: request → outbox.dispatch → worker share a trace."""
    tracer = otel.get_tracer()
    with tracer.start_as_current_span("test.request") as request_span:
        request_context = request_span.get_span_context()
        # submit_job injects the *active* context (this span) into the outbox row.
        submit_job(
            job_type="property_csv_import",
            payload={"source": "sample:properties.csv"},
            idempotency_key=None,
        )

    # Relay re-hydrates that context; eager Celery runs process_job (and its ingest) inline.
    assert dispatch_outbox() == 1

    finished = {span.name: span for span in spans.get_finished_spans()}
    dispatch_span = finished["outbox.dispatch"]
    # Same trace as the original request, and a direct child of the request span — the
    # persisted-then-rehydrated context bridged the transactional-outbox gap.
    assert dispatch_span.context.trace_id == request_context.trace_id
    assert dispatch_span.parent.span_id == request_context.span_id
    # The worker's ingest span rides the same trace end-to-end.
    assert finished["ingest"].context.trace_id == request_context.trace_id


def test_dispatch_without_trace_context_still_works(spans):
    """A pre-tracing / direct-call outbox row (no active span) dispatches on a fresh root."""
    submit_job(
        job_type="property_csv_import",
        payload={"source": "sample:properties.csv"},
        idempotency_key=None,
    )
    assert dispatch_outbox() == 1

    dispatch_span = next(s for s in spans.get_finished_spans() if s.name == "outbox.dispatch")
    # No caller context to inherit → the dispatch span starts a new root (no parent).
    assert dispatch_span.parent is None


def test_configure_tracing_is_noop_when_disabled(settings, monkeypatch):
    """OTEL_ENABLED=false: configure_tracing returns early, installing nothing."""
    monkeypatch.setattr(otel, "_configured", False)
    settings.OTEL_ENABLED = False
    otel.configure_tracing("foreman-web")
    assert otel._configured is False


def test_configure_tracing_installs_provider_when_enabled(settings, monkeypatch):
    """OTEL_ENABLED=true builds a provider and instruments the stack (side effects stubbed)."""
    settings.OTEL_ENABLED = True
    settings.OTEL_SAMPLER_RATIO = 1.0
    monkeypatch.setattr(otel, "_configured", False)
    # Stub the global side effects so the test doesn't mutate the process-wide provider or
    # patch Django/Celery/psycopg for the rest of the suite.
    installed: dict = {}
    monkeypatch.setattr(otel.trace, "set_tracer_provider", lambda p: installed.setdefault("p", p))
    monkeypatch.setattr(otel, "OTLPSpanExporter", InMemorySpanExporter)
    for name in ("DjangoInstrumentor", "CeleryInstrumentor", "PsycopgInstrumentor"):
        monkeypatch.setattr(otel, name, lambda: _NoopInstrumentor())

    otel.configure_tracing("foreman-worker")

    assert otel._configured is True
    assert isinstance(installed["p"], TracerProvider)
    installed["p"].shutdown()
    otel._configured = False  # don't leak the flag to other tests


class _NoopInstrumentor:
    def instrument(self) -> None:
        pass
