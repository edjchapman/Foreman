# LISTEN/NOTIFY push-dispatch for the outbox

Load testing showed end-to-end latency dominated by **queue wait** (p95 ≈ 2.1 s)
— the structural cost of the Beat poll, not saturation. So a Postgres
`AFTER INSERT` trigger on the outbox table fires `pg_notify` and a dedicated
listener process dispatches on each notification, collapsing submit → first
claim from p50 733 ms to 41 ms. **Beat stays as the durability fallback**,
because NOTIFY is a wakeup, not a delivery guarantee — a notification raised
while no listener is connected is simply lost; the outbox row remains the
durable truth, and with the listener down the system degrades to exactly the
previous behaviour.

## Considered Options

- **App-level `transaction.on_commit` notify** — the trigger inherits NOTIFY's
  transactional delivery for free and *cannot be forgotten* by a future writer
  of outbox rows.
- **Lowering the Beat poll interval** — trades latency for idle DB load; the
  floor is the polling model itself.

## Consequences

- Push-dispatch is a latency layer on top of at-least-once, never a
  replacement; removing the listener is a safe, behaviour-preserving rollback.
- Retries are deliberately *not* pushed — a retry is already intentionally
  delayed by its backoff.
- The trigger is guarded to PostgreSQL (SQLite test runs skip it), matching the
  `SKIP LOCKED` fencing pattern. Before/after data:
  [docs/load-testing.md](../load-testing.md).
