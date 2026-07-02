# Contributing to Foreman

## Commit & PR conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/):

    <type>[(<scope>)][!]: <subject>

- **type** (required, lowercase): `feat` `fix` `docs` `style` `refactor`
  `perf` `test` `build` `ci` `chore` `revert`
- **scope** (optional): a sub-area, e.g. `feat(api):`, `build(makefile):`
- **!** marks a breaking change · keep the subject ≤ 72 chars

PRs are **squash-merged**, so the **PR title becomes the permanent commit
subject** — write it to the standard above. This is **enforced**: `commit-style.yml`
runs `--strict` and is a required check on the `main` ruleset, so a non-conforming
PR title blocks the merge.

## Quality gates

- `make check` — docs/hygiene gate: internal markdown links + anchors. Runs in CI
  (`check.yml`, on every PR/push plus a weekly scheduled drift check) and locally
  on commit. Needs only bash, python3, git.
- `make ci` — stack gate: ruff lint/format-check + pytest. Needs `uv` and a
  Postgres `DATABASE_URL` (see `.env.example`). Runs in `ci.yml`.
- `make lint` / `make fmt` / `make test` — individual stack steps.
- `make preflight` — the full pre-PR gate (`ci` + `audit` + `check`); run it
  before opening a PR.

## From merge to production

Merging a PR is the last manual step. On every push to `main`, release-please
maintains a Release PR that accumulates the Conventional-Commit changes;
merging *that* cuts a GitHub Release, publishes
`ghcr.io/edjchapman/foreman:<version>` (with SLSA provenance), and deploys it
to Railway — web first, gated by pre-deploy `migrate` + `/readyz`, then
worker/beat. The pipeline is diagrammed in [docs/ci.md](docs/ci.md); deploy
topology and rollback live in [docs/deploy.md](docs/deploy.md).

## Git hooks

Commit hooks run via your global Git hooks dispatcher (secret-scanning + `ruff`
+ `make check`). No repo-local `core.hooksPath` is set, so global protections stay
active. The vendored `.githooks/` directory documents the standalone hooks used by
other repos in this tooling family; it is inert here by design.
