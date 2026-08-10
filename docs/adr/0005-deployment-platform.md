# Deployment platform: Railway, one image, semver-pinned CD

The demo runs on **Railway** — web (daphne), worker, and beat as three services
from the one attested GHCR image, with Postgres/Redis as container services —
because usage-based billing (~$8–15/mo, ~$5 floor when destroyed) fits an
always-on, near-zero-traffic demo where flat per-service pricing triples the
cost. CD never tracks `:latest`: each release pins the exact semver tag via
Railway's GraphQL API, web deploys first (pre-deploy `migrate`, `/readyz`
gating cutover) before worker/beat, so new worker code never runs ahead of its
migrations. The platform is declared in Terraform; an idempotent script covers
the three settings the community provider can't express, making
`terraform destroy`/`apply` the demo's off/on switch.

## Considered Options

- **Render** — true managed Postgres with PITR, but ~3× the cost for this shape.
- **Fly.io** — best deploy mechanics, but managed-Postgres economics ($38/mo floor).
- **Tracking `:latest` / Railway repo builds** — non-reproducible, and CD should
  ship the already-built, attested, integration-tested artifact, not rebuild it.

## Consequences

- **No PITR** — daily volume snapshots only; a bad write between snapshots is
  unrecoverable. Accepted for demo data any sample job regenerates.
- Hybrid IaC (Terraform + configure script) rather than pure; state is local
  and git-ignored (it holds generated secrets).
- The $10 hard usage cap can take the demo offline rather than overspend — deliberate.
