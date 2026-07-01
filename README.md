# Foreman

[![CI](https://github.com/edjchapman/foreman/actions/workflows/ci.yml/badge.svg)](https://github.com/edjchapman/foreman/actions/workflows/ci.yml)
[![CodeQL](https://github.com/edjchapman/foreman/actions/workflows/codeql.yml/badge.svg)](https://github.com/edjchapman/foreman/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/edjchapman/foreman/badge)](https://securityscorecards.dev/viewer/?uri=github.com/edjchapman/foreman)
[![codecov](https://codecov.io/gh/edjchapman/foreman/branch/main/graph/badge.svg)](https://codecov.io/gh/edjchapman/foreman)
[![Release](https://img.shields.io/github/v/release/edjchapman/foreman?sort=semver)](https://github.com/edjchapman/foreman/releases)
[![License: MIT](https://img.shields.io/github/license/edjchapman/foreman)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](pyproject.toml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-checked-2a6db2.svg)](https://mypy-lang.org/)
[![Conventional Commits](https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow.svg)](https://www.conventionalcommits.org)

**Event-driven job-processing platform** — a property-data import & report-generation service that demonstrates backend **reliability engineering** *beyond CRUD*: at-least-once delivery, exactly-once *effect*, failure isolation, and automatic crash recovery.

Submit a job (e.g. a property CSV import) → the API records it atomically and emits a domain event through a **transactional outbox** → **idempotent workers** process it with **retries** and a **dead-letter** path, recovering on their own from a worker crash → progress streams over **WebSockets** *(M4)* before a downloadable report.

> Portfolio project — the focus is the *reliability and operability* story, not feature breadth. **Milestones 1–4 are complete** — through observability, live job status over WebSockets ([ADR 0004](docs/adr/0004-realtime-websockets.md)), and a minimal live-progress demo page at `/`; **M5 (deploy + public demo) is next.**

## Contents

- [Highlights](#highlights)
- [Architecture](#architecture)
- [Reliability model](#reliability-model)
- [Tech stack](#tech-stack)
- [Quickstart](#quickstart)
- [API](#api)
- [Engineering practices](#engineering-practices)
- [Roadmap](#roadmap)
- [Development](#development)
- [License](#license)

## Highlights

- **Transactional outbox** — a job and its domain event commit in a single DB transaction, so the system never publishes a message for a job that didn't persist, and never persists a job whose event was lost (no dual-write race). See [ADR 0001](docs/adr/0001-transactional-outbox.md).
- **Exactly-once *effect*** — workers are idempotent via a per-job natural key + `bulk_create(ignore_conflicts=True)`, so an at-least-once redelivery converges on the same rows instead of duplicating them.
- **Retries, backoff & dead-letter** — transient failures retry with capped, full-jitter exponential backoff; poison inputs fail fast; exhausted jobs land in a dead-letter state an operator can `redrive`. See [ADR 0002](docs/adr/0002-retries-dlq-lease.md).
- **Crash recovery** — a worker lease + reaper reclaim a job whose worker died mid-flight, and a **fencing token** stops a resumed zombie worker from clobbering the row.
- **Non-blocking, horizontal claims** — the relay and requeue scans use `SELECT … FOR UPDATE SKIP LOCKED`, so parallel workers claim disjoint rows without contending.
- **Observable & operable** — structured JSON logs at every state transition, DB-derived Prometheus metrics (queue depth, dispatch lag, dead-letter count) on `/metrics`, split liveness/readiness probes, and an operator [runbook](docs/runbook.md). See [ADR 0003](docs/adr/0003-observability.md).
- **Live status over WebSockets** — a job's `PENDING → … → terminal` transitions stream to `ws/jobs/<id>/` via Django Channels (snapshot on connect, then deltas). The fan-out is **best-effort**, so the realtime layer never changes whether a job succeeds. See [ADR 0004](docs/adr/0004-realtime-websockets.md).
- **Run like a service** — `mypy --strict`, ruff (incl. bandit), a 90% coverage floor against a real PostgreSQL, ADRs, automated releases, and a hardened supply chain (see [Engineering practices](#engineering-practices)).

## Architecture

```mermaid
flowchart LR
    client([Client]) -->|"POST /api/v1/jobs/"| api[DRF API]
    subgraph tx["Single DB transaction"]
        api --> job[("Job: PENDING")]
        api --> outbox[("OutboxEvent: PENDING")]
    end
    beat[Celery Beat] -->|"every N s"| relay[Outbox relay]
    relay -->|"claim PENDING (SKIP LOCKED)"| outbox
    relay -->|publish| broker[("Redis broker")]
    broker --> worker[Celery worker]
    worker -->|"idempotent natural key"| effect["CSV import → PropertyRecord"]
    worker -->|"transient failure"| retry{"Retry? capped + jittered backoff"}
    retry -->|"attempts left"| broker
    retry -->|exhausted| dlq[("Dead-letter")]
    reaper[Lease reaper] -->|"reclaim expired lease"| worker
    operator([Operator]) -->|redrive| dlq
    worker -->|"→ SUCCEEDED / FAILED"| job
    job -->|"live status (WebSocket)"| client
```

The **outbox** decouples submission from dispatch; the **relay** is a dumb publisher that never re-reads the job (so it can't race it); the **worker** owns idempotency, retries, and terminal state. See [ADR 0001](docs/adr/0001-transactional-outbox.md) and [ADR 0002](docs/adr/0002-retries-dlq-lease.md) for the rationale.

## Reliability model

The design is organised around explicit delivery and failure guarantees:

| Concern | Guarantee | Mechanism |
|---|---|---|
| Publish | **No dual-write** | Job + `OutboxEvent` commit in one transaction; a Beat relay publishes the outbox. |
| Delivery | **At-least-once** | The relay re-sends after a crash between publish and mark-dispatched. |
| Effect | **Exactly-once** | Per-job natural key + `bulk_create(ignore_conflicts=True)` — reprocessing converges, never duplicates. |
| Transient failure | **Retry, then dead-letter** | Capped full-jitter exponential backoff; dead-letter after `JOB_MAX_ATTEMPTS`; operator `redrive`. |
| Poison input | **Fail fast** | An `IngestError` goes straight to `FAILED` with no retries. |
| Worker crash | **Lease + reaper recovery** | An expired lease is reclaimed; a fencing token discards a resumed zombie's stale write. |
| Concurrency | **Non-blocking claims** | `SELECT … FOR UPDATE SKIP LOCKED` (PostgreSQL). |

Failure modes and the crash-window analysis are documented in [ADR 0002](docs/adr/0002-retries-dlq-lease.md).

## Tech stack

Python 3.12 · Django 6 + Django REST Framework · PostgreSQL 16 · Redis + Celery · structured logging + Prometheus metrics · Django Channels / WebSockets · Docker Compose · pytest · GitHub Actions.

## Quickstart

Full stack with Docker:

```bash
make up          # full stack + live demo UI at http://localhost:8000
```

On the host with [uv](https://docs.astral.sh/uv/) (no Docker — reads `DATABASE_URL` from your env, see `.env.example`):

```bash
uv sync
make migrate
make test
uv run python manage.py runserver
```

Submit and track a job:

```bash
curl -X POST localhost:8000/api/v1/jobs/ \
  -H 'Content-Type: application/json' \
  -d '{"job_type": "property_csv_import", "payload": {"source": "sample:properties.csv"}}'

curl localhost:8000/api/v1/jobs/<id>/
curl localhost:8000/healthz   # liveness
curl localhost:8000/readyz    # readiness (DB + broker)
curl localhost:8000/metrics   # Prometheus metrics

# stream a job's live status (needs websocat)
websocat ws://localhost:8000/ws/jobs/<id>/
```

The default sample source (`sample:properties.csv`) resolves to a bundled fixture, so a job runs end-to-end with no external storage — watch it move `PENDING → PROCESSING → SUCCEEDED`, with `progress` and an import summary in `result`. Inline CSV via `payload.csv` also works; remote schemes (`s3://`, `https://`) are a later milestone.

## API

The current API is `v1`:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/jobs/` | Submit a job → `202 Accepted` with id + `Location`. Honours an `Idempotency-Key` header. |
| `GET` | `/api/v1/jobs/{id}/` | Job status, progress, result, error. |
| `GET` | `/api/v1/jobs/` | List jobs (paginated). |
| `GET` | `/healthz` | Liveness — the process is up (no dependency I/O). |
| `GET` | `/readyz` | Readiness — database + broker reachable (`503` if not). |
| `GET` | `/metrics` | Prometheus metrics — queue depth, dispatch lag, dead-letter count. |
| `WS` | `/ws/jobs/{id}/` | Live status/progress stream — snapshot on connect, then deltas. |

A submitted job is recorded `PENDING` alongside its outbox event in one transaction; the relay publishes it and the worker drives it to `SUCCEEDED` (or `FAILED`).

## Engineering practices

Beyond the feature work, the repo is operated like a production service:

- **CI gates** (required on `main`): ruff lint + format, `mypy --strict` with django/DRF stubs, and pytest at a **90% coverage floor** against a real PostgreSQL service — plus a docs/link gate. Run the whole thing locally with `make preflight`.
- **Security & supply chain**: CodeQL (code *and* workflows), dependency review on PRs, scheduled `pip-audit`, secret scanning + push protection, SHA-pinned actions, a digest-pinned Docker base image, and SLSA build-provenance attestations on release images. Posture is graded by [OpenSSF Scorecard](https://securityscorecards.dev/viewer/?uri=github.com/edjchapman/foreman).
- **Automated releases**: Conventional Commits drive [release-please](https://github.com/googleapis/release-please) — it maintains the `CHANGELOG` + version and cuts GitHub Releases, each publishing a versioned image to **GHCR** (`ghcr.io/edjchapman/foreman`).
- **Governance**: protected `main` (required checks, linear history, squash-only, no bypass), `CODEOWNERS`, issue templates, a [security policy](SECURITY.md), and Dependabot across Python, Actions, and Docker.
- **Decisions**: architecture choices are captured as [ADRs](docs/adr/README.md).

## Roadmap

- **M1 — walking skeleton** *(done)*: repo, Docker Compose, `Job` model, submit/track API, health check, tests + CI.
- **M2 — async worker + transactional outbox** *(done)*: atomic job+event write, Beat relay, worker ingests the property CSV into `PropertyRecord`. See [ADR 0001](docs/adr/0001-transactional-outbox.md).
- **M3 — reliability** *(done)*: worker-side idempotency (exactly-once effect), retries with backoff, dead-letter, lease-based crash recovery, operator redrive, documented failure modes. See [ADR 0002](docs/adr/0002-retries-dlq-lease.md).
- **M4 — realtime UI + observability** *(done)*: observability (structured logging, DB-derived Prometheus metrics, liveness/readiness, [runbook](docs/runbook.md); [ADR 0003](docs/adr/0003-observability.md)), **live job status over WebSockets** (Channels; [ADR 0004](docs/adr/0004-realtime-websockets.md)), and a **minimal live-progress demo page** at `/` (vanilla JS, no build step).
- **M5 — ship**: Cypress E2E, deploy + public demo, case study.

## Development

`make help` lists every target; **`make preflight`** runs the full pre-PR gate (lint + types + tests + audit + docs). Worker/relay run locally via `make worker` / `make beat` (or `make relay` for a one-shot outbox dispatch).

Contributions follow [Conventional Commits](https://www.conventionalcommits.org) (the PR title is enforced by a required check and becomes the squash-merge subject). See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © Ed Chapman.
