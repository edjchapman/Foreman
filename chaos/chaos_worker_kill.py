#!/usr/bin/env python3
"""Chaos: SIGKILL the worker mid-job and prove lease-reaper recovery end to end.

Boots an isolated compose stack (project ``foreman-chaos`` — fresh volumes, no host
ports except web:8000), submits a mix of instant imports and slow ``fault:sleep:``
imports, SIGKILLs the worker while the slow ones are mid-``process_job``, restarts
it, and asserts the reliability contract held:

- every job reaches SUCCEEDED within the deadline (nothing lost);
- every job's CSV report has exactly the sample's 5 rows (nothing duplicated — the
  ``(job, external_id)`` constraint held under crash + broker redelivery);
- each job that was PROCESSING at kill time recovered via a fresh claim
  (``attempts >= 2``);
- for every job, ``job.claimed`` log lines == final ``attempts`` — the redelivered
  unacked message hit the PROCESSING no-op guard instead of double-claiming, which
  is the ``acks_late``/``reject_on_worker_lost`` contract observed for real.

Excluded from `make ci` (needs Docker); run via `make chaos` from the repo root.
Set CHAOS_KEEP=1 to leave the stack up for a post-mortem. See chaos/README.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

BASE_URL = os.environ.get("CHAOS_BASE_URL", "http://localhost:8000")
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "foreman-chaos",
    "-f",
    "docker-compose.yml",
    "-f",
    "chaos/compose.chaos.yml",
]
FAST_JOBS = 20
SLEEP_JOBS = 3
NAP_SECONDS = 8  # must stay under the chaos JOB_LEASE_SECONDS (10) — see compose.chaos.yml
BOOT_DEADLINE_S = 300
RECOVERY_DEADLINE_S = 240
SAMPLE_ROWS = 5
TERMINAL = {"SUCCEEDED", "FAILED", "DEAD_LETTER"}


class ChaosError(AssertionError):
    """A reliability property did not hold (or the harness lost its kill window)."""


def compose(*args: str, capture: bool = False) -> str:
    result = subprocess.run([*COMPOSE, *args], check=True, text=True, capture_output=capture)
    # `compose logs` relays container stderr (where Celery logs) on stderr — keep both.
    return (result.stdout or "") + (result.stderr or "") if capture else ""


def api_json(path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    request = urllib.request.Request(f"{BASE_URL}{path}", data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return dict(json.loads(response.read()))


def api_text(path: str) -> str:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as response:
        return str(response.read().decode())


def wait_until(deadline_s: float, poll, what: str, interval_s: float = 1.0) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if poll():
            return
        time.sleep(interval_s)
    raise ChaosError(f"timed out after {deadline_s:.0f}s waiting for {what}")


def stack_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/readyz", timeout=3) as response:
            return bool(response.status == 200)
    except OSError:
        return False


def submit(source: str) -> str:
    body = {"job_type": "property_csv_import", "payload": {"source": source}}
    return str(api_json("/api/v1/jobs/", body)["id"])


def status_of(job_id: str) -> str:
    return str(api_json(f"/api/v1/jobs/{job_id}/")["status"])


def all_terminal(job_ids: list[str]) -> bool:
    return all(status_of(job_id) in TERMINAL for job_id in job_ids)


def kill_worker_mid_nap(slow: list[str]) -> list[str]:
    """SIGKILL the worker while sleep jobs are inside their nap; return their ids."""
    processing: list[str] = []

    def snapshot() -> bool:
        processing[:] = [job_id for job_id in slow if status_of(job_id) == "PROCESSING"]
        return bool(processing)

    wait_until(60, snapshot, "a fault:sleep job to reach PROCESSING", interval_s=0.5)
    time.sleep(2)  # settle well inside the 8s nap window
    snapshot()  # refresh right before the kill — this set is the assertion target
    if not processing:
        raise ChaosError("kill window missed — no sleep job was PROCESSING at kill time")
    compose("kill", "-s", "SIGKILL", "worker")
    return list(processing)


def verify(job_ids: list[str], killed: list[str]) -> None:
    jobs = {job_id: api_json(f"/api/v1/jobs/{job_id}/") for job_id in job_ids}

    wrong = {job_id: j["status"] for job_id, j in jobs.items() if j["status"] != "SUCCEEDED"}
    if wrong:
        raise ChaosError(f"jobs did not all succeed: {wrong}")
    print(f"chaos: all {len(jobs)} jobs SUCCEEDED")

    unrecovered = [job_id for job_id in killed if jobs[job_id]["attempts"] < 2]
    if unrecovered:
        raise ChaosError(f"killed jobs finished without a fresh claim: {unrecovered}")
    print(f"chaos: {len(killed)} killed job(s) recovered via a fresh claim (attempts >= 2)")

    _verify_reports(job_ids)
    _verify_claim_counts(jobs)


def _verify_reports(job_ids: list[str]) -> None:
    """Every report must hold exactly the sample's rows — none lost, none duplicated."""
    for job_id in job_ids:
        rows = api_text(f"/api/v1/jobs/{job_id}/report/").strip().splitlines()
        data_rows = len(rows) - 1  # header
        if data_rows != SAMPLE_ROWS:
            raise ChaosError(f"job {job_id}: report has {data_rows} rows, want {SAMPLE_ROWS}")
    print(f"chaos: every report has exactly {SAMPLE_ROWS} rows — no lost or duplicated records")


def _verify_claim_counts(jobs: dict) -> None:
    """job.claimed log lines == attempts: the redelivered message never double-claimed."""
    logs = compose("logs", "worker", capture=True).splitlines()
    for job_id, detail in jobs.items():
        claims = sum(1 for line in logs if job_id in line and "job.claimed" in line)
        if claims != detail["attempts"]:
            raise ChaosError(
                f"job {job_id}: {claims} claim log lines vs attempts={detail['attempts']}"
                " — a redelivered message may have raced a live claim"
            )
    print("chaos: claim log lines match attempts for every job — no double live claim")


def dump_logs() -> None:
    for service in ("worker", "beat"):
        tail = compose("logs", "--tail", "80", service, capture=True)
        print(f"\n===== {service} logs (last 80 lines) =====\n{tail}", file=sys.stderr)


def main() -> int:
    print("chaos: booting isolated stack (compose project foreman-chaos)...", flush=True)
    compose("up", "-d", "--build")
    try:
        wait_until(BOOT_DEADLINE_S, stack_ready, "stack readiness (/readyz)")

        fast = [submit("sample:properties.csv") for _ in range(FAST_JOBS)]
        slow = [submit(f"fault:sleep:{NAP_SECONDS}") for _ in range(SLEEP_JOBS)]
        print(f"chaos: submitted {len(fast)} fast + {len(slow)} fault:sleep:{NAP_SECONDS} jobs")

        killed = kill_worker_mid_nap(slow)
        print(f"chaos: SIGKILLed the worker with {len(killed)} job(s) mid-process")

        compose("up", "-d", "worker")
        wait_until(
            RECOVERY_DEADLINE_S,
            lambda: all_terminal(fast + slow),
            "all jobs to reach a terminal state",
            interval_s=2,
        )
        verify(fast + slow, killed)
    except (ChaosError, subprocess.CalledProcessError, OSError) as failure:
        print(f"\nchaos: FAIL — {failure}", file=sys.stderr)
        dump_logs()
        return 1
    finally:
        if os.environ.get("CHAOS_KEEP") == "1":
            print("chaos: CHAOS_KEEP=1 — leaving the stack up for post-mortem")
        else:
            compose("down", "-v")
    print("\nchaos: PASS — a SIGKILLed worker lost nothing and duplicated nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
