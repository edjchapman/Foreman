"""Smoke test for the M2 Celery scaffolding — no job behaviour yet."""

from config import celery, celery_app
from jobs.tasks import ping


def test_celery_app_configured():
    assert celery_app.main == "foreman"


def test_ping_runs_eagerly():
    # _eager_celery (autouse) makes .delay() execute inline and return a result.
    result = ping.delay()
    assert result.get() == "pong"


def test_tracing_signal_handlers_configure_without_error():
    """The worker/beat tracing hooks call configure_tracing; a no-op when OTEL is off (M7)."""
    # These fire from Celery's worker_process_init / beat_init in a real (forked) process —
    # invoke them directly so the fork-time wiring is exercised in the suite. See ADR 0008.
    celery._init_worker_tracing()
    celery._init_beat_tracing()
