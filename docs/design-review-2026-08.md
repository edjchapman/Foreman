# Design review — August 2026

A deep-module review of the codebase: for each module, how much behaviour sits
behind how much interface, where the seams are, and whether tests cross the
same seam callers do. **Findings only** — the recommendations at the end are
ranked but deliberately not applied; each is small enough to land as its own
PR when wanted.

Vocabulary, used consistently below: a **module** is anything with an interface
and an implementation; its **interface** is everything a caller must know to
use it correctly (signatures, invariants, ordering rules, error modes — not
just types); a module is **deep** when a lot of behaviour hides behind a small
interface; a **seam** is where an interface lives. Depth buys callers
**leverage** (more behaviour per unit of interface learned) and maintainers
**locality** (change concentrates in one place).

Scope: all of `jobs/` and `config/`, the test suite's choice of test surfaces,
and the two wire shapes (the outbox event payload and the WebSocket envelope).

## What's deep and working

Credit first, because the best modules here are genuinely good and worth
imitating when touching the weaker ones.

- **[`jobs/metrics.py`](../src/jobs/metrics.py)** is the deepest module in the
  repo. Eight Prometheus series — gauges, monotonic counters, and DB-side
  bucketed histograms — sit behind a single scrape interface, derived entirely
  from Postgres at collection time. There is **not one `.inc()` or
  `.observe()` call site anywhere in the codebase**: a new code path is
  observable by construction, costing its author nothing and leaving nothing
  to forget. That is locality by design ([ADR 0006](adr/0006-load-testing-metrics.md)).
- **[`config/otel.py`](../src/config/otel.py)** hides the whole OpenTelemetry
  SDK/instrumentor setup behind four functions (`configure_tracing`,
  `get_tracer`, `inject_trace`, `span_from_carrier`). The outbox trace bridge
  — inject the carrier at write time (`jobs/services.py:46`), re-hydrate it at
  dispatch (`jobs/tasks.py:86`) — crosses a gap that no auto-instrumentation
  can, for a two-function interface ([ADR 0008](adr/0008-opentelemetry-tracing.md)).
- **[`jobs/ingest.py`](../src/jobs/ingest.py)** is the only module in the repo
  tested purely through its public interface
  ([`test_ingest.py`](../src/jobs/tests/test_ingest.py) calls `load_csv_text` and
  `parse_rows`, nothing else). The docstring's "swappable processing seam"
  claim mostly holds — the exception is finding 3.
- **`submit_job`** ([`jobs/services.py`](../src/jobs/services.py)) passes the
  deletion test: delete it and the Job+OutboxEvent atomicity invariant — the
  whole point of the outbox — reappears at every submission site. One small
  function, one invariant, one home.

## Finding 1 — the state machine has no module

The `Job` status walk (`PENDING → PROCESSING → SUCCEEDED | FAILED |
DEAD_LETTER`, plus the retry/reap edges back to `PENDING`) is the core domain
behaviour, and it is the one piece of behaviour with no module of its own. The
transitions are enforced across roughly ten sites:

| Transition | Enforced at |
|---|---|
| `PENDING → PROCESSING` (claim + lease) | `jobs/tasks.py:137` (`_claim_pending`) |
| `PROCESSING → SUCCEEDED / FAILED` | `jobs/tasks.py:195` (`_terminal`, via `_fenced_update:220`) |
| `PROCESSING → PENDING` (backoff retry) | `jobs/tasks.py:172` (`_handle_transient`) |
| `PROCESSING → PENDING / DEAD_LETTER` (lease reaped) | `jobs/tasks.py:340` (`_recover_one_lease`) |
| `DEAD_LETTER → PENDING` (redrive) | `jobs/services.py:60` (`redrive_dead_letter`) |
| Preconditions restated | `jobs/views.py:69` (report needs `SUCCEEDED`), `:88` (redrive needs `DEAD_LETTER`) |

Because the rules have no single home, they are spelled repeatedly:

- The **"release the lease" field set** (`leased_until=None, lease_token=None,
  …`) is written out independently **four times** — `jobs/tasks.py:183-188`,
  `:206-208`, `:349-354`, and `jobs/services.py:70-75`.
- The **dead-letter rule** (`attempts >= JOB_MAX_ATTEMPTS`) appears **twice**
  (`jobs/tasks.py:174` and `:342`) — `_handle_transient` and
  `_recover_one_lease` are the same transition with different delays, written
  as different code.
- The **terminal-status set** is defined independently **four times**
  in-process ([`jobs/retention.py`](../src/jobs/retention.py) line 32,
  [`jobs/metrics.py`](../src/jobs/metrics.py) lines 52–56, plus the chaos and
  Locust scripts) and again in the demo page's JS.

`redrive_dead_letter` is the sharpest symptom: it is a state transition living
in a different module, using a different mechanism (bulk `.update()`, no
fencing — correctly, as it happens, since a `DEAD_LETTER` row holds no live
lease) and, as finding 2 shows, sitting outside the notification regime the
other transitions follow.

None of this is wrong today — the concurrency suite proves the fenced paths
work. The cost is that every rule has N places to drift apart, and the next
transition added (pause? cancel?) has no interface to conform to.

## Finding 2 — broadcast is a caller obligation, and one caller forgot

`notify_job` ([`jobs/realtime.py`](../src/jobs/realtime.py)) is the single
sync→async broadcast seam ([ADR 0004](adr/0004-realtime-websockets.md)) — good.
But *calling* it is a per-transition obligation: nine call sites (eight in
`jobs/tasks.py`, one in `jobs/views.py:96`), and each caller must also know
whether its write needs `transaction.on_commit` wrapping (three do, six don't).
That ordering rule lives in the callers' heads, not in any interface.

The predictable failure has already happened. `redrive_dead_letter` doesn't
notify, so each of its three callers must remember to compensate:

- [`jobs/views.py`](../src/jobs/views.py) lines 94–96 remembers
  (`refresh_from_db()` + `notify_job`);
- [`jobs/admin.py`](../src/jobs/admin.py) lines 27–29 and
  [`manage.py redrive`](../src/jobs/management/commands/redrive.py) **do not** —
  an admin or CLI redrive is invisible to every connected WebSocket client,
  including the live queue board, until some other transition happens to fire.

`test_broadcast_seams.py` (since deleted — see recommendation A's status note)
existed solely to police this discipline transition-by-transition — a test suite
standing in for a guarantee the design could make structurally (see
recommendation A: if every transition ends by scheduling its own broadcast,
the discipline, the compensating view code, and most of that test file
disappear together).

## Finding 3 — the ingest seam is split

`ingest.load_csv_text` dispatches on the payload (`csv` inline, `sample:`,
`https://`) — but the fourth scheme, `fault:`, is dispatched *outside* the
seam, in `jobs/tasks.py:249-257` (`_import_properties` branches on
`is_fault_source` itself). That falsifies
[`jobs/faults.py`](../src/jobs/faults.py)'s explicit claim (lines 24–25) to be
"the only place that knows about `fault:` sources", and it means payload-shape
knowledge (`payload["source"]`) is read in two modules and constructed in at
least five places across production code, fixtures, and the demo JS.

The split has a real cause: `load_fault_csv` needs `job.attempts` and
`job.created_at`, which `load_csv_text`'s payload-only signature can't carry.
The fix is a job-level resolver in `ingest.py`, not a wider payload function
(recommendation B).

Related: the **permanent-vs-transient failure taxonomy** — `IngestError`
subclass ⇒ fail fast, anything else ⇒ retry with backoff
([ADR 0002](adr/0002-retries-dlq-lease.md)) — is genuinely part of the ingest
interface, but it is expressible only as a class-identity convention that
three modules (`ingest.py`, `sources.py`, `faults.py`) must independently
honour and that only `jobs/tasks.py:112-118` enforces. The contract is real;
its home is nowhere.

## Finding 4 — `tasks.py` is tested past its interface

The interface is the test surface: callers and tests should cross the same
seam. [`jobs/tasks.py`](../src/jobs/tasks.py) — at 372 lines the largest module —
exposes four Celery tasks, but its most valuable behaviour (claim, lease,
fence, backoff, reap) is only reachable as private helpers, so the tests reach
past the interface:

- [`test_reliability.py`](../src/jobs/tests/test_reliability.py) and
  [`test_concurrency.py`](../src/jobs/tests/test_concurrency.py) call
  `_claim_pending`, `_reap_expired_leases`, `_terminal`, and `_retry_delay`
  directly, and monkeypatch `_import_properties` and `random.uniform`.

When the best tests in the suite (these are the ones that prove the
lease-fencing race) can only express themselves against private names, the
module is the wrong shape: the reliability engine wants an interface of its
own. Notably, [`jobs/retention.py`](../src/jobs/retention.py) was already split
out of `tasks.py` on exactly this kind of pressure — the precedent exists.

## Finding 5 — the wire shapes have no owner in the tests

Two dict shapes cross process boundaries, and in both cases the producer and
consumer are never tested against each other:

- **WebSocket group envelope** `{"type", "data", "trace"}`: built at
  `jobs/realtime.py:57-58`, parsed at `jobs/consumers.py:45-48` and `:77-80` —
  and hand-duplicated in
  [`test_consumers.py`](../src/jobs/tests/test_consumers.py) (lines 66–69 and
  97–100), which drives the consumers with hand-built dicts instead of
  `notify_job`. A shape drift between producer and consumer would break
  production while both halves' tests stay green.
- **Outbox event payload** `{"job_id", "trace"}`: constructed correctly only
  at `jobs/services.py:43-47`, parsed only at `jobs/tasks.py:86-87`, asserted
  in exactly one test — while three test files build `OutboxEvent` rows by
  hand with no `trace` (or no payload at all).

A naming wrinkle worth knowing when reading the realtime code: the string
`"queue.job"` names **two different shapes** — the channel-layer group message
(payload under `"data"`) and the client-facing frame the consumer re-wraps it
into (payload under `"job"`).

## Minor observations

- [`jobs/models.py`](../src/jobs/models.py) is pure declaration — no managers, no
  methods; all behaviour is external, including a behavioural piece of
  `OutboxEvent` (the `pg_notify` trigger) that lives invisibly in
  [migration 0006](../src/jobs/migrations/0006_outbox_notify_trigger.py).
- `reports.stream_report`'s `SUCCEEDED` precondition lives in the view
  (`jobs/views.py:69`); called directly, the module happily streams a failed
  job's partial rows. The filename comes from the module but the
  `Content-Disposition` header is assembled in the view — one contract, two
  homes.
- `jobs/serializers.py:35` reaches into `Job._meta` for a field default —
  fallout from `submit_job`'s keyword signature being fed by a
  `**validated_data` splat in the view.
- `config/otel.py` keeps a `_configured` module global that
  `test_tracing.py` reads and resets — private state acting as a test
  interface.
- The health checks (`check_database`, `check_broker`) are module-level
  functions inside `views.py`, unit-tested by name — public-ish behaviour
  without a module.

## Recommendations, ranked by leverage per churn

None of these are applied. Order is by behaviour gained per line changed;
sizes are estimates from a design pass against the current source.

### A. A `jobs/lifecycle.py` deep module (~190 lines, mostly code motion)

> **Status: implemented** — `jobs/lifecycle.py` now owns every transition (plus
> `record_progress` and the requeue claim, beyond the sketch below); the locking
> helper became a shared `LockingQuerySet` on the models, fenced writes return
> `bool`, and creation also broadcasts. Line references above describe the
> pre-change code.

One module owning every legal `Job` transition and its persistence rules:

```python
claim(job_id) -> Job | None          # PENDING → PROCESSING, fresh lease
succeed(job, *, result)              # fenced terminal write
fail(job, *, error)                  # fenced terminal write (poison input)
retry_or_dead_letter(job, *, error, delay_seconds=None)
reap_expired_leases() -> int
redrive(job_ids) -> int              # folds in services.redrive_dead_letter
retry_delay(attempts) -> float       # the documented backoff curve
```

This absorbs all four lease-reset spellings and both dead-letter-rule sites,
and — the key move — **every transition ends with
`transaction.on_commit(lambda: notify_job(job))` unconditionally** (Django
runs on-commit callbacks immediately when no transaction is active, so the
one line is correct in both contexts). Notification becomes a property of the
transition instead of a caller obligation, which fixes the silent admin/CLI
redrive as a side effect and lets `views.py`'s compensation and most of
`test_broadcast_seams.py` be deleted. Dispatch (`dispatch_outbox`,
`process_job`, the requeue scan's `.delay()` half) stays in `tasks.py`: the
state machine should not know how messages travel. The private-poking tests
in finding 4 become public-interface calls with near-zero rewrites.

### B–D, and what not to do

- **D — `Job.TERMINAL_STATUSES` on the model** (~6 lines): the model is the
  one module `retention.py` and `metrics.py` already import.
  Deliberately *don't* chase the copies in `chaos/`, `load/`, or the demo JS —
  those are out-of-process HTTP clients, and re-spelling three strings is the
  honest cost of a network seam.
- **C — test the WS contract through its real producer** (~30 test-only
  lines): drive `notify_job` over the in-memory channel layer into a
  `WebsocketCommunicator`, replacing the hand-built envelopes; add a
  `services.outbox_event(job)` constructor and make the tests use it. After
  this, each wire shape exists in exactly one production spelling that the
  tests share.
- **B — `ingest.resolve_csv(job)`** (~25 lines net): one resolver owning all
  four schemes (`fault:` joins via a deferred `faults` import, mirroring the
  existing deferred `sources` import); `_import_properties` shrinks to three
  lines; a `TransientIngestError` base in `ingest.py` gives the failure
  taxonomy one declared home.

Explicitly rejected, with reasons: an FSM library or transition table (five
transitions with heterogeneous side effects gain nothing from a table — the
table becomes a second interface to learn); Django signals for broadcast
(re-hides the seam behind implicit registration and makes on-commit ordering
invisible); fencing on `redrive` (the missing piece was notification —
`DEAD_LETTER` rows hold no lease, the status filter *is* the guard); lifecycle
as manager/instance methods (instance-plus-fence-token transitions don't
compose as querysets, and splitting one interface across two surfaces reduces
depth); a TypedDict/schema layer for the wire dicts (one producer function
each plus an end-to-end test is fewer moving parts than a schema module).
