# Contributing to Foreman

## Commit & PR conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/):

    <type>[(<scope>)][!]: <subject>

- **type** (required, lowercase): `feat` `fix` `docs` `style` `refactor`
  `perf` `test` `build` `ci` `chore` `revert`
- **scope** (optional): a sub-area, e.g. `feat(api):`, `build(makefile):`
- **!** marks a breaking change · keep the subject ≤ 72 chars

PRs are **squash-merged**, so the **PR title becomes the permanent commit
subject** — write it to the standard above. Validation is **warn-only** today
(`commit-style.yml` warns on a non-conforming PR title but does not block).

## Quality gates

- `make check` — docs/hygiene gate: internal markdown links + anchors. Runs in CI
  (`check.yml`, weekly `scheduled-check.yml`) and locally on commit. Needs only
  bash, python3, git.
- `make ci` — stack gate: ruff lint/format-check + pytest. Needs `uv` and a
  Postgres `DATABASE_URL` (see `.env.example`). Runs in `ci.yml`.
- `make lint` / `make fmt` / `make test` — individual stack steps.

## Git hooks

Commit hooks run via your global Git hooks dispatcher (secret-scanning + `ruff`
+ `make check`). No repo-local `core.hooksPath` is set, so global protections stay
active. The vendored `.githooks/` directory documents the standalone hooks used by
other repos in this tooling family; it is inert here by design.
