#!/usr/bin/env bash
# Deploy ghcr.io/edjchapman/foreman:<version> to Railway (web -> worker -> beat).
#
# Railway does not watch GHCR: pushing a new tag deploys nothing, and
# `railway redeploy` re-runs the *previous* deployment with its original image
# reference. So CD is: pin each service's image to the exact semver tag
# (serviceInstanceUpdate), then create a fresh deployment
# (serviceInstanceDeployV2) via the public GraphQL API. Semver pinning keeps
# every Railway deployment reproducible and makes dashboard rollback
# meaningful (an old deployment re-runs the old version).
#
# ORDERING IS LOAD-BEARING: web deploys first and must reach SUCCESS before
# worker/beat are touched. Web's pre-deploy command runs `manage.py migrate`
# and its /readyz healthcheck gates the cutover — so new worker code never
# runs ahead of its migrations. A failed web deploy aborts the whole rollout
# (Railway keeps the previous deployment serving). Worker and beat then deploy
# concurrently and are each polled to SUCCESS — a crash-looping worker fails
# the rollout instead of leaving CD green.
#
# ENV CONTRACT (all required):
#   RAILWAY_TOKEN              project token (env-scoped) — sent as
#                              Project-Access-Token. Set RAILWAY_TOKEN_KIND=account
#                              to send an account token as Authorization: Bearer.
#   RAILWAY_ENVIRONMENT_ID     the production environment id
#   RAILWAY_WEB_SERVICE_ID     service ids (dashboard URL or `railway status --json`)
#   RAILWAY_WORKER_SERVICE_ID
#   RAILWAY_BEAT_SERVICE_ID
#
# USAGE: railway-deploy.sh <version>        e.g. railway-deploy.sh 0.7.0
#
# NOTE: Railway returns HTTP 200 for GraphQL-level errors, so every response
# is checked for an `errors` key — curl -f alone is not enough.

set -euo pipefail

VERSION="${1:?usage: railway-deploy.sh <version>}"
IMAGE_REPO="edjchapman/foreman"
IMAGE="ghcr.io/${IMAGE_REPO}:${VERSION}"
API="https://backboard.railway.com/graphql/v2"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-600}"
POLL_SECONDS=10

: "${RAILWAY_TOKEN:?RAILWAY_TOKEN is required}"
: "${RAILWAY_ENVIRONMENT_ID:?RAILWAY_ENVIRONMENT_ID is required}"
: "${RAILWAY_WEB_SERVICE_ID:?RAILWAY_WEB_SERVICE_ID is required}"
: "${RAILWAY_WORKER_SERVICE_ID:?RAILWAY_WORKER_SERVICE_ID is required}"
: "${RAILWAY_BEAT_SERVICE_ID:?RAILWAY_BEAT_SERVICE_ID is required}"

if [[ "${RAILWAY_TOKEN_KIND:-project}" == "account" ]]; then
  AUTH_HEADER="Authorization: Bearer ${RAILWAY_TOKEN}"
else
  AUTH_HEADER="Project-Access-Token: ${RAILWAY_TOKEN}"
fi

check_image_exists() { # fail before pinning anything if the tag was never published
  local token
  token="$(curl -fsS "https://ghcr.io/token?scope=repository:${IMAGE_REPO}:pull" | jq -r '.token')"
  curl -fsSI -o /dev/null \
    -H "Authorization: Bearer ${token}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/${IMAGE_REPO}/manifests/${VERSION}" || {
    echo "${IMAGE} not found on GHCR — is ${VERSION} a published release?" >&2
    return 1
  }
}

gql() { # gql <json-body> -> response body (fails on transport or GraphQL errors)
  local body response
  body="$1"
  response="$(curl -sSf "$API" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$body")"
  if jq -e '.errors' <<<"$response" >/dev/null 2>&1; then
    echo "GraphQL error: $(jq -c '.errors' <<<"$response")" >&2
    return 1
  fi
  printf '%s' "$response"
}

set_image() { # set_image <service-id>
  gql "$(jq -n --arg s "$1" --arg e "$RAILWAY_ENVIRONMENT_ID" --arg i "$IMAGE" '{
    query: "mutation($s:String!,$e:String!,$in:ServiceInstanceUpdateInput!){serviceInstanceUpdate(serviceId:$s,environmentId:$e,input:$in)}",
    variables: {s: $s, e: $e, in: {source: {image: $i}}}
  }')" >/dev/null
}

deploy() { # deploy <service-id> -> the created deployment's id
  gql "$(jq -n --arg s "$1" --arg e "$RAILWAY_ENVIRONMENT_ID" '{
    query: "mutation($s:String!,$e:String!){serviceInstanceDeployV2(serviceId:$s,environmentId:$e)}",
    variables: {s: $s, e: $e}
  }')" | jq -r '.data.serviceInstanceDeployV2'
}

deployment_status() { # deployment_status <deployment-id>
  gql "$(jq -n --arg d "$1" '{
    query: "query($d:String!){deployment(id:$d){status}}",
    variables: {d: $d}
  }')" | jq -r '.data.deployment.status // "UNKNOWN"'
}

latest_deployment_id() { # latest_deployment_id <service-id>
  gql "$(jq -n --arg s "$1" --arg e "$RAILWAY_ENVIRONMENT_ID" '{
    query: "query($s:String!,$e:String!){deployments(first:1,input:{serviceId:$s,environmentId:$e}){edges{node{id}}}}",
    variables: {s: $s, e: $e}
  }')" | jq -r '.data.deployments.edges[0].node.id // empty'
}

# Polls the SPECIFIC deployment created by deploy() — "latest deployment of
# the service" is racy: the new record can lag the mutation, so a first poll
# may read a PREVIOUS (possibly failed) deployment and abort a healthy rollout.
wait_success() { # wait_success <deployment-id> <service-id> <label>
  local dep deadline status
  dep="$1"
  deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    status="$(deployment_status "$dep")"
    case "$status" in
      SUCCESS)
        echo "$3: deployment SUCCESS"
        return 0
        ;;
      FAILED | CRASHED)
        echo "$3: deployment $status — aborting rollout" >&2
        return 1
        ;;
      REMOVED)
        # The image re-pin and the explicit deploy can race into a pair of
        # deployments; Railway keeps one and REMOVEs the other. Track the
        # survivor rather than treating supersession as failure.
        dep="$(latest_deployment_id "$2")"
        [[ -n "$dep" ]] || { echo "$3: deployment REMOVED with no successor — aborting" >&2; return 1; }
        echo "$3: deployment superseded — tracking $dep"
        sleep "$POLL_SECONDS"
        ;;
      *)
        echo "$3: $status …"
        sleep "$POLL_SECONDS"
        ;;
    esac
  done
  echo "$3: timed out after ${WAIT_TIMEOUT_SECONDS}s — aborting rollout" >&2
  return 1
}

echo "Deploying ${IMAGE}"
check_image_exists

echo "web: pinning image + deploying (pre-deploy migrate + /readyz gate run here)"
set_image "$RAILWAY_WEB_SERVICE_ID"
WEB_DEP="$(deploy "$RAILWAY_WEB_SERVICE_ID")"
wait_success "$WEB_DEP" "$RAILWAY_WEB_SERVICE_ID" "web"

echo "worker: pinning image + deploying"
set_image "$RAILWAY_WORKER_SERVICE_ID"
WORKER_DEP="$(deploy "$RAILWAY_WORKER_SERVICE_ID")"

echo "beat: pinning image + deploying"
set_image "$RAILWAY_BEAT_SERVICE_ID"
BEAT_DEP="$(deploy "$RAILWAY_BEAT_SERVICE_ID")"

# Worker/beat deploy concurrently; polling is serial but the deadline in
# wait_success is per-service, so a slow worker doesn't eat beat's budget.
wait_success "$WORKER_DEP" "$RAILWAY_WORKER_SERVICE_ID" "worker"
wait_success "$BEAT_DEP" "$RAILWAY_BEAT_SERVICE_ID" "beat"

echo "Rollout of ${VERSION} complete — web, worker, and beat all verified."
