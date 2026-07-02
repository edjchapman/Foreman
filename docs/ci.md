# CI/CD pipeline

How a change travels from PR to the [live platform](https://foreman-demo.up.railway.app).
This is the overview; the deploy half (topology, provisioning, rollback) is
detailed in [deploy.md](deploy.md) and decided in
[ADR 0005](adr/0005-deployment-platform.md).

```mermaid
flowchart LR
    pr([Pull request]) --> gates
    subgraph gates["PR gates"]
        ci["ci — ruff · mypy --strict · pytest\n(90% floor, real Postgres) · terraform validate"]
        check["check — markdown links + anchors"]
        style["commit-style — Conventional-Commit PR title"]
        dr["dependency-review — vulnerable dep diff"]
        cq["codeql — Python + workflow analysis"]
    end
    gates -->|squash-merge| main[(main)]
    main --> rp["release-please\nmaintains the Release PR"]
    rp -->|"merge Release PR"| rel([Release + tag])
    rel --> img["publish-image\nGHCR + SLSA provenance"]
    img --> dep["deploy\npin semver tag on Railway"]
    dep --> web["web\npre-deploy migrate + /readyz gate"]
    web --> wb["worker + beat\npolled to SUCCESS"]
```

Merging a PR is the last manual step: release-please turns the merged
Conventional Commits into a Release PR, and merging *that* cuts the release,
publishes `ghcr.io/edjchapman/foreman:<x.y.z>`, and rolls it out — web first
(its pre-deploy `migrate` and `/readyz` healthcheck gate the fleet), then
worker and beat, each polled to `SUCCESS` so a crash-looping service fails the
rollout instead of leaving CD green.

## Workflows

Every workflow delegates to a `make` target — the YAML holds triggers and
permissions, never build logic — so each gate runs identically in CI and
locally (`make preflight` = `ci` + `audit` + `check`).

| Workflow | Triggers | What it gates |
|---|---|---|
| `ci.yml` | PR, push | `make ci` — ruff, `mypy --strict`, pytest with a 90% coverage floor against a real Postgres service; `make tf-check` validates the Terraform module; coverage uploads to Codecov (OIDC). |
| `check.yml` | PR, push, weekly | `make check` — markdown link + anchor validators; the scheduled run catches out-of-band drift. |
| `commit-style.yml` | PR | PR title vs Conventional Commits (`scripts/check-commit-msg.sh --strict`) — the title becomes the squash-merge subject. |
| `dependency-review.yml` | PR | Blocks newly-introduced vulnerable or license-incompatible dependencies. |
| `codeql.yml` | PR, push, weekly | Static analysis of the Python app **and the workflows themselves**. |
| `audit.yml` | PR, push, weekly | `make audit` — pip-audit over the locked deps; the schedule reddens CI when a CVE lands post-merge. |
| `scorecard.yml` | push, weekly | OpenSSF Scorecard — supply-chain posture, powers the README badge. |
| `release-please.yml` | push to `main` | Release PR → GitHub Release → GHCR image (+ SLSA provenance) → calls `deploy.yml`. |
| `deploy.yml` | called by release, manual | `make deploy VERSION=<x.y.z>` — GHCR pre-flight, semver pin, gated rollout. The manual path covers rollback and post-rebuild re-pinning. |

## Conventions

- **Actions are SHA-pinned** (with `# vX` comments); Dependabot keeps the pins
  fresh across Python, Actions, and Docker ecosystems.
- **Least-privilege tokens** — every workflow declares a `permissions:` block;
  the deploy workflows start from `permissions: {}`.
- **Every job has a `timeout-minutes`** — a hung runner can't burn the 6-hour default.
- **Deploys never interleave** — the release and manual deploy paths share a
  `railway-deploy` concurrency group with `cancel-in-progress: false`.
- **Releases are reproducible** — CD pins exact semver image tags, never
  `:latest`, so a dashboard rollback re-runs a known version.
