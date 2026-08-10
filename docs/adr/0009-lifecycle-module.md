# The lifecycle module owns every Job transition

Job transitions were spelled across ~10 sites in `tasks.py`/`services.py`, and
broadcasting was a per-caller obligation that admin and CLI redrive had already
forgotten. Now `src/jobs/lifecycle.py` is the single home for every `Job` write:
each transition persists, emits its structured log event, and schedules its own
`transaction.on_commit` broadcast — inseparably, so notification is a property
of the transition, not a caller discipline. Fenced-out writes (a reaped
worker's stale lease token) return `False`, log `job.write_fenced`, and stay
silent — the live owner announces its own state. Callers never invoke
`notify_job`.

## Considered Options

- **An FSM library / transition table** — five transitions with heterogeneous
  side effects gain nothing from a table; the table becomes a second interface
  to learn.
- **Django signals for broadcast** — re-hides the seam behind implicit
  registration and makes on-commit ordering invisible.
- **Manager/instance methods on `Job`** — instance-plus-fence-token transitions
  don't compose as querysets, and splitting one interface across two surfaces
  reduces depth.

## Consequences

- Creation broadcasts too (`submit_job`), so the push-only queue board shows
  PENDING arrivals — previously invisible until claim, exactly wrong during a
  backlog.
- The silent admin/CLI redrive was fixed structurally, and the per-seam
  broadcast test file dissolved into interface-level contract tests
  (`src/jobs/tests/test_lifecycle.py`).
- Amends [ADR 0004](0004-realtime-websockets.md), whose caller-side broadcast
  mechanics this replaces. Executed in
  [PR #150](https://github.com/edjchapman/Foreman/pull/150).
