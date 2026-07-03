#!/usr/bin/env bash
# One-shot post-`terraform apply` configuration for the three deploy settings
# the Terraform provider (v0.6.x) cannot express, set via the same
# serviceInstanceUpdate mutation the CD script uses for image pinning:
#
#   web    → Pre-Deploy Command  (python manage.py migrate)
#            Healthcheck Path    (/readyz)
#   worker → Custom Start Command (celery worker)
#   beat   → Custom Start Command (celery beat)
#
# The image puts the venv on PATH (see Dockerfile), so these invoke the binaries
# directly — the runtime image no longer ships `uv`.
#
# Settings live on the service instance and take effect from the NEXT
# deployment (the release workflow's `make deploy`, or a manual deploy) — this
# script does not trigger one. Idempotent: safe to re-run any time.
#
# ENV CONTRACT (same as railway-deploy.sh):
#   RAILWAY_TOKEN              project token (env-scoped) — sent as
#                              Project-Access-Token. Set RAILWAY_TOKEN_KIND=account
#                              to send an account token as Authorization: Bearer.
#   RAILWAY_ENVIRONMENT_ID     optional — when unset, all four IDs are read from
#   RAILWAY_WEB_SERVICE_ID       `terraform output -json github_ci_variables`
#   RAILWAY_WORKER_SERVICE_ID    in deploy/terraform (needs local state).
#   RAILWAY_BEAT_SERVICE_ID
#
# USAGE: terraform -chdir=deploy/terraform apply && ./scripts/railway-configure.sh

set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/terraform" && pwd)"

: "${RAILWAY_TOKEN:?RAILWAY_TOKEN is required}"

if [[ -z "${RAILWAY_ENVIRONMENT_ID:-}" ]]; then
  echo "Reading service ids from terraform output (${TF_DIR})"
  ids="$(terraform -chdir="$TF_DIR" output -json github_ci_variables)"
  RAILWAY_ENVIRONMENT_ID="$(jq -re '.RAILWAY_ENVIRONMENT_ID' <<<"$ids")"
  RAILWAY_WEB_SERVICE_ID="$(jq -re '.RAILWAY_WEB_SERVICE_ID' <<<"$ids")"
  RAILWAY_WORKER_SERVICE_ID="$(jq -re '.RAILWAY_WORKER_SERVICE_ID' <<<"$ids")"
  RAILWAY_BEAT_SERVICE_ID="$(jq -re '.RAILWAY_BEAT_SERVICE_ID' <<<"$ids")"
  # Optional — present only after the listener service is added (ADR 0007).
  RAILWAY_LISTENER_SERVICE_ID="$(jq -r '.RAILWAY_LISTENER_SERVICE_ID // empty' <<<"$ids")"
fi
: "${RAILWAY_WEB_SERVICE_ID:?RAILWAY_WEB_SERVICE_ID is required}"
: "${RAILWAY_WORKER_SERVICE_ID:?RAILWAY_WORKER_SERVICE_ID is required}"
: "${RAILWAY_BEAT_SERVICE_ID:?RAILWAY_BEAT_SERVICE_ID is required}"

# Shared Railway plumbing: RAILWAY_API, AUTH_HEADER (token-kind aware), gql().
# shellcheck source=scripts/_lib-railway.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib-railway.sh"

configure() { # configure <service-id> <ServiceInstanceUpdateInput json> <label>
  gql "$(jq -n --arg s "$1" --arg e "$RAILWAY_ENVIRONMENT_ID" --argjson in "$2" '{
    query: "mutation($s:String!,$e:String!,$in:ServiceInstanceUpdateInput!){serviceInstanceUpdate(serviceId:$s,environmentId:$e,input:$in)}",
    variables: {s: $s, e: $e, in: $in}
  }')" >/dev/null
  echo "$3: configured"
}

# preDeployCommand is [String!] in the schema; start/healthcheck are plain strings.
configure "$RAILWAY_WEB_SERVICE_ID" '{
  "preDeployCommand": ["python manage.py migrate"],
  "healthcheckPath": "/readyz"
}' "web (pre-deploy migrate + /readyz healthcheck)"

configure "$RAILWAY_WORKER_SERVICE_ID" '{
  "startCommand": "celery -A config worker -l info --concurrency 2"
}' "worker (celery start command)"

configure "$RAILWAY_BEAT_SERVICE_ID" '{
  "startCommand": "celery -A config beat -l info"
}' "beat (celery start command)"

# Optional push-dispatch listener (ADR 0007) — only if its service has been provisioned.
if [[ -n "${RAILWAY_LISTENER_SERVICE_ID:-}" ]]; then
  configure "$RAILWAY_LISTENER_SERVICE_ID" '{
    "startCommand": "python manage.py outbox_listener"
  }' "listener (outbox push-dispatch start command)"
fi

echo "Deploy settings applied — they take effect from the next deployment of each service."
