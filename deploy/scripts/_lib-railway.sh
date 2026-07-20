#!/usr/bin/env bash
# Shared Railway GraphQL plumbing.
#
# Sourced by `railway-deploy.sh` (CD: image pinning + gated rollout) and
# `railway-configure.sh` (post-`terraform apply` service settings). Single
# source of truth for the API endpoint, token handling, and error checking —
# the same drift-prevention rationale as _lib-stale-branches.sh: if each
# script carried its own copy, a fix to one (e.g. the GraphQL error check)
# could silently miss the other.
#
# EXPECTS: RAILWAY_TOKEN validated by the caller BEFORE sourcing. Honors
# RAILWAY_TOKEN_KIND — a project token (default) is sent as
# Project-Access-Token; set RAILWAY_TOKEN_KIND=account to send an account
# token as Authorization: Bearer.
#
# DEFINES:
#   RAILWAY_API    the public GraphQL endpoint
#   AUTH_HEADER    the auth header matching the token kind
#   gql <json-body>  -> response body (fails on transport or GraphQL errors)
#
# NOTE: Railway returns HTTP 200 for GraphQL-level errors, so gql checks
# every response for an `errors` key — curl -f alone is not enough.

RAILWAY_API="https://backboard.railway.com/graphql/v2"

if [[ "${RAILWAY_TOKEN_KIND:-project}" == "account" ]]; then
  AUTH_HEADER="Authorization: Bearer ${RAILWAY_TOKEN}"
else
  AUTH_HEADER="Project-Access-Token: ${RAILWAY_TOKEN}"
fi

gql() { # gql <json-body> -> response body (fails on transport or GraphQL errors)
  local body response
  body="$1"
  response="$(curl -sSf "$RAILWAY_API" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$body")"
  if jq -e '.errors' <<<"$response" >/dev/null 2>&1; then
    echo "GraphQL error: $(jq -c '.errors' <<<"$response")" >&2
    return 1
  fi
  printf '%s' "$response"
}
