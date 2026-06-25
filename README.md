# Foreman

**Event-driven job-processing platform** — a property-data import & report-generation service built to demonstrate backend reliability engineering *beyond CRUD*.

A user submits a processing job (e.g. a property/lease CSV import); the API records it atomically and emits a domain event via a **transactional outbox**; **idempotent background workers** process it with **retries** and a **dead-letter** path; the UI streams **live progress over WebSockets** before producing a downloadable report.

> Portfolio learning project. The focus is the *reliability and operability* story — at-least-once delivery, exactly-once *effect*, failure isolation, observability — not feature breadth.

## Status

🚧 **Building — Milestone 1 (walking skeleton).** Submit and track a job over a REST API, backed by PostgreSQL, fully containerised and CI-green. Asynchronous processing arrives in M2.

## Stack

Python 3.12 · Django 5 + Django REST Framework · PostgreSQL 16 · Redis + Celery *(M2)* · Django Channels / WebSockets *(M4)* · Docker Compose · pytest · GitHub Actions.

## Quickstart

Full stack with Docker:

```bash
make up          # Django + Postgres; API on http://localhost:8000
```

On the host with uv (no Docker — reads `DATABASE_URL` from your env, see `.env.example`):

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
  -d '{"job_type": "property_csv_import", "payload": {"source": "s3://bucket/sample.csv"}}'

curl localhost:8000/api/v1/jobs/<id>/
curl localhost:8000/healthz
```

## API (v1)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/jobs/` | Submit a job → `202 Accepted` with id + `Location`. Honours an `Idempotency-Key` header. |
| `GET` | `/api/v1/jobs/{id}/` | Job status, progress, result, error. |
| `GET` | `/api/v1/jobs/` | List jobs (paginated). |
| `GET` | `/healthz` | Liveness + database check. |

In M1 a submitted job is recorded as `PENDING` and not processed — the worker that consumes it lands in M2.

## Roadmap

- **M1 — walking skeleton** *(in progress)*: repo, Docker Compose, `Job` model, submit/track API, health check, tests + CI.
- **M2 — async worker + transactional outbox** (Redis + Celery): jobs actually process.
- **M3 — reliability**: worker-side idempotency, retries with backoff, dead-letter, documented failure modes.
- **M4 — realtime UI + observability**: React/TS + live WebSocket progress (Channels), structured logging, runbook.
- **M5 — ship**: Cypress E2E, deploy + public demo, case study.

## Development

`make help` lists every target. Two gates guard the repo:

- **`make ci`** — stack gate: ruff lint + format-check + pytest, against a
  PostgreSQL service. Run by the `ci` workflow.
- **`make check`** — docs/hygiene gate: internal markdown link + anchor
  validation (bash + python3 only). Run by `check` on every push/PR, weekly by
  `scheduled-check`, and locally on commit.

PR titles are linted (warn-only) by `commit-style`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for commit conventions.
