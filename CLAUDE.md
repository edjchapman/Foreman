# Foreman — agent context

Event-driven job-processing platform (portfolio project). Flow: property-data import → **transactional outbox** → **idempotent workers** (retries + dead-letter) → **live WebSocket status** → downloadable report. The point is **backend reliability engineering beyond CRUD**, not feature breadth.

## Stack

Python 3.12, Django 6 + DRF, PostgreSQL 16, Redis + Celery, Django Channels + WebSockets, Docker Compose (daphne/ASGI), structured JSON logging + `prometheus-client` metrics, pytest + pytest-django + pytest-asyncio + factory_boy + pytest-cov, ruff, mypy (+ django/DRF stubs), pip-audit, GitHub Actions.

## Commands

- `make up` / `make down` — local Docker stack (Django + Postgres).
- `make migrate` / `make makemigrations` — migrations (run on the host via uv).
- `make test` — pytest. `make lint` — ruff (check + format). `make typecheck` — mypy (strict; django/DRF stubs; no DB). `make fmt` — auto-fix. `make ci` — lint + typecheck + coverage-gated test (90% floor; what CI runs).
- `make audit` — `pip-audit` for dependency CVEs (own scheduled `audit.yml`). `make preflight` — full pre-PR gate (`ci` + `audit` + `check`).
- `make worker` / `make beat` — Celery worker / Beat (the outbox-relay scheduler). `make relay` — dispatch the outbox once (no Beat).
- `make check` — docs/hygiene gate (markdown link + anchor validators; bash + python3, no DB). Distinct from `make ci` (the stack gate); both run in CI.
- Host runs use `uv`; settings read `DATABASE_URL` from the env (`.env.example`). For a quick host test run without Postgres: `DATABASE_URL="sqlite://:memory:" uv run pytest` (the `select_for_update(skip_locked=True)` locking path is Postgres-only — feature-guarded so SQLite runs, exercised for real in CI).

## Layout

- `config/` — Django project. Settings are env-driven; the DB comes from `DATABASE_URL` via `dj-database-url` (Postgres by default). Structured JSON logging via `config/logformat.py` (`DJANGO_LOG_FORMAT=console` for human-readable dev logs); see ADR 0003.
- `config/celery.py` — Celery app; `config/__init__.py` exposes `celery_app` for autodiscovery. Celery/Redis settings are env-driven (`REDIS_URL`, `CELERY_*`); Beat schedules the outbox relay.
- `jobs/` — the core app. `Job` model: UUID pk; states `PENDING → PROCESSING → SUCCEEDED|FAILED|DEAD_LETTER`; outbox-ready fields `idempotency_key` (unique-or-null) and `attempts`. `OutboxEvent` (transactional outbox) and `PropertyRecord` (imported rows). DRF `JobViewSet` (create/retrieve/list); `HealthView` (liveness `/healthz`), `ReadinessView` (`/readyz` — DB + broker), and `metrics.py` (`/metrics` — DB-derived Prometheus gauges). Realtime: `consumers.py`/`routing.py` stream live status over WebSockets, `realtime.py`'s `notify_job` is the sync→async broadcast boundary, and `config/asgi.py` is a Channels `ProtocolTypeRouter` (ADR 0004).
  - `services.py` — `submit_job` writes `Job` + `OutboxEvent` atomically.
  - `tasks.py` — `dispatch_outbox` (Beat relay, claims PENDING rows with `SKIP LOCKED`) and `process_job` (worker: PENDING→PROCESSING→SUCCEEDED|FAILED).
  - `ingest.py` — CSV source resolution + parsing (the swappable processing seam; `sample:` fixtures and inline `payload.csv`).
  - Tests use the `api_client` fixture and an autouse `_eager_celery` fixture (both in `conftest.py`) so tasks run inline without a broker.

## Milestone roadmap

M1 walking skeleton (submit/track API, no processing) → M2 worker + transactional outbox → M3 reliability (worker-side idempotency, retries, DLQ) → M4 realtime UI + observability → M5 ship (deploy, demo, case study).

## Conventions

- **CI calls `make` targets** — don't inline build/test logic in the workflow YAML.
- DB tests are marked `pytestmark = pytest.mark.django_db`; use `factory_boy` (`JobFactory`) for fixtures. pytest runs strict (`--strict-markers --strict-config`, `filterwarnings=error`), so a new marker must be registered and a new warning fails CI.
- **Realtime/async**: consumers are async (`jobs/consumers.py`); tests use pytest-asyncio `auto` mode + an autouse InMemory channel layer, and `WebsocketCommunicator` consumer tests are **Postgres-only** (`database_sync_to_async`'s second connection can't share in-memory SQLite). The only sync→async crossing is `jobs/realtime.notify_job` — never touch the ORM/serializer inside a consumer; `on_commit` broadcast seams are tested with `django_capture_on_commit_callbacks`. daphne serves ASGI (prod `CMD` + dev `runserver`).
- **Typing**: mypy runs `strict` (one relaxation, `disallow_any_generics`, for JSON-shaped payloads) with django/DRF-stubs; annotate new app code. Tests are mypy-excluded (factory_boy's metaclass return type defeats inference). Ruff carries a broad set incl. `S` (bandit) + `BLE`; its version lives only in `uv.lock` (a local pre-commit hook runs `uv run ruff`).
- Settings stay Postgres-by-default; only point `DATABASE_URL` at SQLite for fast local test runs (no hidden divergence in settings).
- PRs squash-merge — the PR title becomes the permanent commit subject; follow **Conventional Commits** (see [CONTRIBUTING.md](CONTRIBUTING.md)). The `commit-style` workflow lints the PR title (warn-only).
- New milestones land as their own branch + PR; keep the `Job` schema forward-compatible to avoid migration churn.
- CI gates: `make ci` (stack: ruff + mypy + pytest, 90% floor), `make audit` (dependency CVEs, scheduled), and `make check` (docs/hygiene: markdown links + anchors) — run `make preflight` before a PR. The Makefile gate targets, validator `scripts/`, `.githooks/`, and the `check`/`commit-style`/`scheduled-check`/`audit` workflows are vendored shared tooling — edit freely; they're the repo's now.
- `.claude/settings.json` and `.claude/hooks/` are committed (shared — Claude Code on the web clones with no global config, so it needs them). The personal bits stay git-ignored: `.claude/{agents,commands,skills,rules,settings.local.json}` and `.mcp.json`.
