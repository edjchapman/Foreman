"""Locust load harness — drives the submit → outbox → worker pipeline under load.

The point is to generate real submissions so the reliability signals become
*observable* rather than merely argued. While this runs, scrape `/metrics` and
watch:

- `foreman_jobs_processed_total{status="SUCCEEDED"}` climb  → throughput (`rate()`)
- `foreman_job_processing_seconds` / `_queue_wait_seconds`  → p50/p95/p99 latency
- `foreman_outbox_pending` + `_oldest_pending_age_seconds`  → relay backlog under load
- `foreman_jobs{status="PROCESSING"}`                       → worker concurrency

Run via `make load` (see load/README.md); `FOREMAN_LOAD_URL` retargets (default
http://localhost:8000). Deliberately outside `make ci`: it needs a live platform
with Redis + Celery workers, exactly like the `e2e/` suite. Locust is a dev-only
dependency (the `load` group in pyproject.toml), never shipped in the image.

No CSRF token is sent: the API's DRF SessionAuthentication only enforces CSRF for
session-authenticated requests, and a fresh Locust client is anonymous.
"""

from __future__ import annotations

import os
import time

from locust import HttpUser, between, task

# A valid sample import — resolves to a bundled fixture via jobs.ingest, so every
# submission is real work the workers actually process (5 PropertyRecords).
SAMPLE_JOB = {"job_type": "property_csv_import", "payload": {"source": "sample:properties.csv"}}

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "DEAD_LETTER"}

# Bound the end-to-end poll so a stuck pipeline fails the sample instead of hanging.
LIFECYCLE_TIMEOUT_S = float(os.environ.get("FOREMAN_LOAD_LIFECYCLE_TIMEOUT", "60"))
LIFECYCLE_POLL_INTERVAL_S = 0.5


class ForemanUser(HttpUser):
    """Submits imports; mostly fire-and-forget, occasionally follows one to the end."""

    # Modest per-user pacing; scale load with -u / -r on the command line, not here.
    wait_time = between(0.1, 0.5)

    @task(10)
    def submit_import(self) -> None:
        """The primary load: POST a job and confirm it was accepted (202)."""
        with self.client.post(
            "/api/v1/jobs/",
            json=SAMPLE_JOB,
            name="POST /api/v1/jobs/",
            catch_response=True,
        ) as resp:
            if resp.status_code == 202:
                resp.success()
            else:
                resp.failure(f"expected 202 Accepted, got {resp.status_code}")

    @task(1)
    def submit_and_await(self) -> None:
        """Follow one job to a terminal state, timing the full client-observed latency.

        Reported as the synthetic "end-to-end lifecycle" request in Locust's stats,
        so its p95 sits alongside the raw submit latency.
        """
        resp = self.client.post("/api/v1/jobs/", json=SAMPLE_JOB, name="POST /api/v1/jobs/")
        if resp.status_code != 202:
            return
        job_id = resp.json()["id"]
        detail_url = f"/api/v1/jobs/{job_id}/"

        started = time.monotonic()
        while (elapsed := time.monotonic() - started) < LIFECYCLE_TIMEOUT_S:
            status = self.client.get(detail_url, name="GET /api/v1/jobs/[id]/").json()["status"]
            if status in TERMINAL_STATUSES:
                self._record_lifecycle(elapsed * 1000, succeeded=status == "SUCCEEDED")
                return
            time.sleep(LIFECYCLE_POLL_INTERVAL_S)
        self._record_lifecycle(elapsed * 1000, succeeded=False, note="timed out")

    def _record_lifecycle(
        self, response_time_ms: float, *, succeeded: bool, note: str = ""
    ) -> None:
        """Emit a synthetic Locust request so end-to-end latency shows in the stats."""
        self.environment.events.request.fire(
            request_type="LIFECYCLE",
            name="submit → terminal",
            response_time=response_time_ms,
            response_length=0,
            exception=None if succeeded else RuntimeError(note or "did not succeed"),
            context={},
        )
