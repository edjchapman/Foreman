# src/ layout: platform code separated from repository configuration

The repo root mixed four platform entries with ~25 tooling/process entries, so
a reviewer couldn't find the code in one hop. The platform now lives under
`src/` (`config/`, `jobs/`, `manage.py`, `conftest.py`), the live-stack suites
under `tests/` (`e2e/`, `load/`, `chaos/` — everything that needs a running
platform and is excluded from `make ci`), and platform operation under `ops/`
(`deploy/`, `observability/`). Everything left at the root is repository
configuration. The layout is path-based (`[tool.uv] package = false` stands —
this is an application, not a library): tools point at `src/` via
`pythonpath`/`mypy_path`, and nothing is installed.

## Considered Options

- **Flat layout (status quo, Django's default)** — rejected: the 4:25 ratio of
  code to scaffolding at the root buried the platform.
- **Renaming `config/`** — rejected: under `src/` the location already says
  "code"; the name is the Two Scoops convention, and renaming would rewrite
  `DJANGO_SETTINGS_MODULE`, every import, and the Railway start commands held
  outside git.
- **Packaged src/ (editable install)** — rejected: reverses the documented
  application-not-a-library stance for import-isolation guarantees that
  protect libraries, not applications.

## Consequences

- The container layout is deliberately unchanged: the Dockerfile copies
  `src/` to `/app` (`COPY src/ ./`), so the daphne `CMD`, healthcheck,
  collectstatic, and the Railway start/pre-deploy commands (`celery -A config
  …`, `python manage.py migrate`) are independent of the repo layout — the
  restructure required zero changes to live platform state. The image also no
  longer ships docs, scripts, or test suites.
- The word "tests" has two homes by design: `src/jobs/tests/` is the
  in-process unit suite (`make ci`, coverage-gated); root `tests/` holds the
  live-stack suites.
- `scripts/` and `standups/` stay at the root: both are wired to cross-repo
  machinery (vendored claude-code-config tooling and `.githooks`; the standup
  workflow) that assumes those paths.
