"""Smoke test for the M2 Celery scaffolding — no job behaviour yet."""

from config import celery_app
from jobs.tasks import ping


def test_celery_app_configured():
    assert celery_app.main == "foreman"


def test_ping_runs_eagerly():
    # _eager_celery (autouse) makes .delay() execute inline and return a result.
    result = ping.delay()
    assert result.get() == "pong"
