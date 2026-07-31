# Foreman

Event-driven job-processing platform: imports are submitted as jobs, relayed
through a transactional outbox to idempotent workers, and observed live. This
glossary is the project's ubiquitous language — definitions only, no
implementation detail. Grown lazily: terms are added when a design session
resolves them.

## Language

### Dispatch

**Listener**:
The process that subscribes to database notifications and push-dispatches the
outbox.

**Push-dispatch**:
Relaying outbox events the moment they commit, woken by a database
notification.
_Avoid_: instant dispatch, LISTEN/NOTIFY dispatch

**Poll-dispatch**:
Relaying outbox events on a fixed schedule.
_Avoid_: polling relay

**Durability fallback**:
The unconditional poll-dispatch that guarantees every event is delivered even
when no listener is running.
_Avoid_: backstop (that's the listener's own sweep)

**Backstop sweep**:
The listener's periodic safety dispatch between notifications. Distinct from
the durability fallback, which works with no listener at all.

### Deployment lifecycle

**Provisioned**:
A service that exists on the platform, whether or not releases are delivered
to it.
_Avoid_: created, set up

**Enabled**:
A provisioned service that the release pipeline deploys on every release.
_Avoid_: promoted, optional, live

**Freeze**:
Withdrawing a service from the release pipeline while its running container
continues untouched. A freeze is not a rollback.

**Stop**:
Terminating a service's running container on the platform.
