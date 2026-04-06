#!/usr/bin/env bash
# Test POST userJobSearch with curl. Need login session (copied from DevTools → Network).
#
#   export UPWORK_AUTHORIZATION='Bearer oauth2v2_int_...'
#   export UPWORK_COOKIE='...'
#   export UPWORK_TENANT_ID='...'
#   ./curl_userJobSearch.sh
#
# Or: set -a && source curl.env && set +a && ./curl_userJobSearch.sh
#
# If there is .auth/storage_state.json (Playwright): automatically load Bearer / Cookie / tenant
# (export_auth_env.py — set env variable still overwrites file).
#
# UPWORK_OUT=file.json  — ghi body JSON ra file; response headers in ra stderr
# UPWORK_VERBOSE=1      — curl -v
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BODY_JSON="${SCRIPT_DIR}/postman_userJobSearch_body.json"

if [[ -f "${SCRIPT_DIR}/.auth/storage_state.json" ]]; then
  eval "$(python3 "${SCRIPT_DIR}/export_auth_env.py")"
fi

if [[ ! -f "${BODY_JSON}" ]]; then
  echo "Missing ${BODY_JSON}" >&2
  exit 1
fi

: "${UPWORK_AUTHORIZATION:?UPWORK_AUTHORIZATION missing — set env or create .auth/storage_state.json}"
: "${UPWORK_COOKIE:?UPWORK_COOKIE missing}"
: "${UPWORK_TENANT_ID:?UPWORK_TENANT_ID missing}"

REFERER="${UPWORK_REFERER:-${UPWORK_WARM_URL:-https://www.upwork.com/nx/search/jobs/?q=spring%20boot&page=1}}"
URL="https://www.upwork.com/api/graphql/v1?alias=userJobSearch"

EXTRA=()
if [[ -n "${UPWORK_VERBOSE:-}" ]]; then
  EXTRA+=(-v)
fi

COMMON=(
  -X POST "${URL}"
  -H "Content-Type: application/json"
  -H "Accept: */*"
  -H "Origin: https://www.upwork.com"
  -H "Referer: ${REFERER}"
  -H "Authorization: ${UPWORK_AUTHORIZATION}"
  -H "Cookie: ${UPWORK_COOKIE}"
  -H "x-upwork-api-tenantid: ${UPWORK_TENANT_ID}"
  -H "x-upwork-accept-language: en-US"
  --data-binary "@${BODY_JSON}"
)

if [[ -n "${UPWORK_OUT:-}" ]]; then
  curl -sS "${EXTRA[@]}" -D /dev/stderr -o "${UPWORK_OUT}" "${COMMON[@]}"
  echo "Recorded body -> ${UPWORK_OUT}" >&2
else
  curl -sS "${EXTRA[@]}" -D - -o - "${COMMON[@]}"
fi
