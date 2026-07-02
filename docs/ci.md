# CI/CD pipeline

How a change travels from PR to the [live platform](https://foreman-demo.up.railway.app).
This is the overview; the deploy half (topology, provisioning, rollback) is
detailed in [deploy.md](deploy.md) and decided in
[ADR 0005](adr/0005-deployment-platform.md).

```mermaid
flowchart LR
    pr([Pull request]) --> gates
    subgraph gates["PR gates"]
        ci["ci — ruff · mypy --strict · pytest<br/>(90% floor, real Postgres) · terraform validate"]
        check["check — markdown links + anchors"]
        cs["commit-style — Conventional-Commit PR title"]
        dr["dependency-review — vulnerable dep diff"]
        cq["codeql — Python + workflow analysis"]
        au["audit — pip-audit over locked deps"]
    end
    gates -->|squash-merge| main[(main)]
    main --> rp["release-please<br/>maintains the Release PR"]
    rp -->|"merge Release PR"| rel([Release + tag])
    rel --> img["publish-image<br/>GHCR + SLSA provenance"]
    img --> dep["deploy<br/>pin semver tag on Railway"]
    dep --> web["web<br/>pre-deploy migrate + /readyz gate"]
    web --> wb["worker + beat<br/>polled to SUCCESS"]
```

Two merges are manual: the feature PR, and the Release PR that release-please
maintains from the merged Conventional Commits. Merging the Release PR is the
ship decision — it cuts the release, publishes
`ghcr.io/edjchapman/foreman:<x.y.z>`, and rolls it out — web first
(its pre-deploy `migrate` and `/readyz` healthcheck gate the fleet), then
worker and beat, each polled to `SUCCESS` so a crash-looping service fails the
rollout instead of leaving CD green.

## Workflows

The build-and-test gates delegate to `make` targets — the YAML holds triggers
and permissions, not build logic — so `make preflight` (= `ci` + `audit` +
`check`) reproduces them locally. The action-powered gates (codeql,
dependency-review, scorecard) and the PR-title check run only in CI.

| Workflow | Triggers | What it gates |
|---|---|---|
| `ci.yml` | PR, push | `make ci` — ruff, `mypy --strict`, pytest with a 90% coverage floor against a real Postgres service; `make tf-check` validates the Terraform module; coverage uploads to Codecov (OIDC). |
| `check.yml` | PR, push, weekly | `make check` — markdown link + anchor validators; the scheduled run catches out-of-band drift. |
| `commit-style.yml` | PR | PR title vs Conventional Commits (`scripts/check-commit-msg.sh --strict`) — the title becomes the squash-merge subject. |
| `dependency-review.yml` | PR | Blocks newly-introduced vulnerable or license-incompatible dependencies. |
| `codeql.yml` | PR, push, weekly | Static analysis of the Python app **and the workflows themselves**. |
| `audit.yml` | PR, push, weekly | `make audit` — pip-audit over the locked deps; the schedule reddens CI when a CVE lands post-merge. |
| `scorecard.yml` | push, weekly, branch-protection changes | OpenSSF Scorecard — supply-chain posture, powers the README badge. |
| `release-please.yml` | push to `main` | Release PR → GitHub Release → GHCR image (+ SLSA provenance) → calls `deploy.yml`. |
| `deploy.yml` | called by release, manual | `make deploy VERSION=<x.y.z>` — GHCR pre-flight, semver pin, gated rollout. The manual path covers rollback and post-rebuild re-pinning. |

## Conventions

- **Actions are SHA-pinned** (with `# vX` comments); Dependabot keeps the pins
  fresh across Python, Actions, and Docker ecosystems.
- **Least-privilege tokens** — every workflow declares a `permissions:` block;
  the deploy workflows start from `permissions: {}`.
- **Every runnable job sets `timeout-minutes`** — a hung runner can't burn the
  6-hour default (a reusable-workflow *call* can't carry one; the called job's
  timeout applies).
- **Deploys never interleave** — the release and manual deploy paths share a
  `railway-deploy` concurrency group with `cancel-in-progress: false`.
- **Releases are reproducible** — CD pins exact semver image tags, never
  `:latest`, so a dashboard rollback re-runs a known version.
