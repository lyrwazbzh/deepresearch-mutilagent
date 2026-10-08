#!/usr/bin/env bash
set -euo pipefail

release_env=${1:?usage: deploy.sh RELEASE_ENV_FILE}
active_env=${ACTIVE_ENV_FILE:-.deploy/active.env}
previous_env=${PREVIOUS_ENV_FILE:-.deploy/previous.env}
mkdir -p "$(dirname "$active_env")"

set -a
. "$release_env"
set +a
python ops/validate_image_refs.py

if [[ -f "$active_env" ]]; then
  cp "$active_env" "$previous_env"
fi
cp "$release_env" "$active_env"

rollback() {
  status=$?
  if [[ -f "$previous_env" ]]; then
    cp "$previous_env" "$active_env"
    docker compose --env-file "$active_env" -f compose.production.yaml up -d --wait
    echo "Deployment failed; previous digest-pinned release restored." >&2
  fi
  exit "$status"
}
trap rollback ERR

docker compose --env-file "$active_env" -f compose.production.yaml \
  --profile migration run --rm migration
docker compose --env-file "$active_env" -f compose.production.yaml up -d --wait
curl --fail --silent --show-error --retry 12 --retry-delay 5 \
  "${DEPLOY_READINESS_URL:?required}/readyz" >/dev/null
trap - ERR
echo "Deployment PASS"
