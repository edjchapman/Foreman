#!/usr/bin/env bash
# Post-deploy smoke probes — the curl-able half of docs/deploy.md's checklist
# (health, readiness, metrics). The browser half (demo flow over the WebSocket,
# CSV report, poison-job FAILED path) is `make e2e`, run right after this in CD.
# FOREMAN_E2E_URL retargets; defaults to the live demo.
set -euo pipefail

BASE_URL="${FOREMAN_E2E_URL:-https://foreman-demo.up.railway.app}"
BASE_URL="${BASE_URL%/}"

echo "smoke: probing ${BASE_URL}"

curl -fsS --max-time 10 "${BASE_URL}/healthz" >/dev/null
echo "smoke: /healthz OK (liveness)"

curl -fsS --max-time 10 "${BASE_URL}/readyz" >/dev/null
echo "smoke: /readyz OK (DB + broker reachable)"

curl -fsS --max-time 10 "${BASE_URL}/metrics" | grep -q "foreman_"
echo "smoke: /metrics exposes foreman_* series"

echo "smoke: all probes green"
