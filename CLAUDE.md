# Foreman — agent context

Event-driven job-processing platform (portfolio project). Flow: property-data import → **transactional outbox** → **idempotent workers** (retries + dead-letter) → **live WebSocket status** → downloadable report. The point is **backend reliability engineering beyond CRUD**, not feature breadth.

## Stack

Python 3.12, Django 5 + DRF, PostgreSQL 16, Redis + Celery, Django Channels (M4+), Docker Compose, pytest + pytest-django + factory_boy + pytest-cov, ruff, GitHub Actions.

## Commands

- `make up` / `make down` — local Docker stack (Django + Postgres).
- `make migrate` / `make makemigrations` — migrations (run on the host via uv).
- `make test` — pytest. `make lint` — ruff check + format-check. `make fmt` — auto-fix. `make ci` — lint + coverage-gated test (80% floor; what CI runs).
- `make worker` / `make beat` — Celery worker / Beat (the outbox-relay scheduler). `make relay` — dispatch the outbox once (no Beat).
- `make check` — docs/hygiene gate (markdown link + anchor validators; bash + python3, no DB). Distinct from `make ci` (the stack gate); both run in CI.
- Host runs use `uv`; settings read `DATABASE_URL` from the env (`.env.example`). For a quick host test run without Postgres: `DATABASE_URL="sqlite://:memory:" uv run pytest` (the `select_for_update(skip_locked=True)` locking path is Postgres-only — feature-guarded so SQLite runs, exercised for real in CI).

## Layout

- `config/` — Django project. Settings are env-driven; the DB comes from `DATABASE_URL` via `dj-database-url` (Postgres by default).
- `config/celery.py` — Celery app; `config/__init__.py` exposes `celery_app` for autodiscovery. Celery/Redis settings are env-driven (`REDIS_URL`, `CELERY_*`); Beat schedules the outbox relay.
- `jobs/` — the core app. `Job` model: UUID pk; states `PENDING → PROCESSING → SUCCEEDED|FAILED|DEAD_LETTER`; outbox-ready fields `idempotency_key` (unique-or-null) and `attempts`. `OutboxEvent` (transactional outbox) and `PropertyRecord` (imported rows). DRF `JobViewSet` (create/retrieve/list) + `HealthView`.
  - `services.py` — `submit_job` writes `Job` + `OutboxEvent` atomically.
  - `tasks.py` — `dispatch_outbox` (Beat relay, claims PENDING rows with `SKIP LOCKED`) and `process_job` (worker: PENDING→PROCESSING→SUCCEEDED|FAILED).
  - `ingest.py` — CSV source resolution + parsing (the swappable processing seam; `sample:` fixtures and inline `payload.csv`).
  - Tests use the `api_client` fixture and an autouse `_eager_celery` fixture (both in `conftest.py`) so tasks run inline without a broker.

## Milestone roadmap

M1 walking skeleton (submit/track API, no processing) → M2 worker + transactional outbox → M3 reliability (worker-side idempotency, retries, DLQ) → M4 realtime UI + observability → M5 ship (deploy, demo, case study).

## Conventions

- **CI calls `make` targets** — don't inline build/test logic in the workflow YAML.
- DB tests are marked `pytestmark = pytest.mark.django_db`; use `factory_boy` (`JobFactory`) for fixtures.
- Settings stay Postgres-by-default; only point `DATABASE_URL` at SQLite for fast local test runs (no hidden divergence in settings).
- PRs squash-merge — the PR title becomes the permanent commit subject; follow **Conventional Commits** (see [CONTRIBUTING.md](CONTRIBUTING.md)). The `commit-style` workflow lints the PR title (warn-only).
- New milestones land as their own branch + PR; keep the `Job` schema forward-compatible to avoid migration churn.
- Two CI gates: `make ci` (stack: ruff + pytest) and `make check` (docs/hygiene: markdown links + anchors). The Makefile gate targets, validator `scripts/`, `.githooks/`, and the `check`/`commit-style`/`scheduled-check` workflows are vendored shared tooling — edit freely; they're the repo's now.
- `.claude/` and `.mcp.json` are personal (machine-specific symlinks / local settings) and git-ignored — never commit them.
