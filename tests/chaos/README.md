# Chaos harness

Executable proof of the crash-recovery contract: `make chaos` SIGKILLs the Celery
worker **mid-`process_job`** against an isolated Docker Compose stack and asserts that
the platform loses nothing and duplicates nothing.

The unit/concurrency suites verify the mechanisms in isolation (`jobs/tests/
test_reliability.py`, `jobs/tests/test_concurrency.py`); this harness verifies the
composed system — broker redelivery (`acks_late` + `reject_on_worker_lost`), the lease
reaper, the requeue lane, and the `(job, external_id)` exactly-once-effect constraint —
under a real worker crash, across real processes.

## What a run does

1. Boots a fresh stack under the compose project `foreman-chaos` (own containers,
   network, and volumes — your dev stack and its data are untouched; only web:8000 is
   published, see [compose.chaos.yml](compose.chaos.yml)).
2. Submits 20 instant `sample:` imports plus 3 `fault:sleep:8` imports — the nap holds
   the worker inside the import window so the kill is deterministic, and stays under
   the chaos lease (10s) so an *unkilled* nap never outlives its own lease.
3. Waits for sleep jobs to reach `PROCESSING`, then `docker compose kill -s SIGKILL worker`.
4. Restarts the worker and polls every job to a terminal state.
5. Asserts:
   - **every** job ends `SUCCEEDED` (nothing lost);
   - every report has exactly the sample's 5 rows (nothing duplicated);
   - each killed job shows `attempts >= 2` (recovered via a fresh claim);
   - per job, `job.claimed` log lines == final `attempts` (the redelivered unacked
     message hit the `PROCESSING` no-op guard — no double live claim).
6. Tears the stack down (`down -v`). Set `CHAOS_KEEP=1` to keep it for a post-mortem.

## Running

```bash
make chaos            # needs Docker; ~2-3 min incl. image build
CHAOS_KEEP=1 make chaos   # leave the stack up on exit
```

On failure the driver prints the last 80 worker/beat log lines and exits non-zero.

Like `e2e/` and `load/`, this is excluded from `make ci` — it needs Docker and real
processes. CI runs it on a nightly schedule (`.github/workflows/chaos.yml`), non-blocking.

## Recovery clocks

[compose.chaos.yml](compose.chaos.yml) shrinks the recovery tunables so the run
completes in seconds (prod defaults in parentheses): `JOB_LEASE_SECONDS=10` (120),
`RECOVER_POLL_SECONDS=1` (5), `JOB_RETRY_BASE_SECONDS=1` (2),
`JOB_REQUEUE_VISIBILITY_SECONDS=15` (60). The *mechanics* under test are unchanged —
only the clocks are faster.
