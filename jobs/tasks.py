"""Celery tasks for the jobs app.

M2 scaffolding ships only `ping` (a wiring smoke test). The outbox relay
(`dispatch_outbox`) and the worker (`process_job`) land in the relay+worker PR.
"""

from __future__ import annotations

from celery import shared_task


@shared_task(name="jobs.ping")
def ping() -> str:
    """Trivial task to confirm Celery autodiscovery and execution are wired."""
    return "pong"
