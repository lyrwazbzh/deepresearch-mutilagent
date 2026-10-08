#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
result_root=${PHASE14_CLOSEOUT_ROOT:?PHASE14_CLOSEOUT_ROOT must be a new persistent directory}
if [[ -e "$result_root" ]]; then
  echo "Refusing to reuse Secret restore result directory: $result_root" >&2
  exit 2
fi
mkdir -p "$result_root"

work_root=$(mktemp -d /tmp/phase14-secret-closeout.XXXXXX)
restore_name=phase14-closeout-secret-pg
secret_ref=datasource/phase14-closeout-20261008
tenant_id=tenant_dev
export PHASE14_TEST_DB_USERNAME=deepresearch
export PHASE14_TEST_DB_PASSWORD=deepresearch

cleanup() {
  if [[ -f "$work_root/recovery/root.key" ]]; then
    "$root/.venv/bin/python" "$root/src/test/acceptance/secret_restore_acceptance.py" cleanup \
      --database-url postgresql+psycopg://deepresearch:deepresearch@localhost:5432/deepresearch \
      --key-file "$work_root/recovery/root.key" \
      --tenant-id "$tenant_id" --secret-ref "$secret_ref" \
      >"$result_root/source-cleanup.json" 2>/dev/null || true
  fi
  docker rm -f "$restore_name" >/dev/null 2>&1 || true
  if [[ "$work_root" == /tmp/phase14-secret-closeout.* ]]; then
    rm -rf -- "$work_root"
  fi
}
trap cleanup EXIT

if docker container inspect "$restore_name" >/dev/null 2>&1; then
  echo "Refusing to replace existing container: $restore_name" >&2
  exit 2
fi

cd "$root"
umask 077
mkdir -p "$work_root/recovery"
openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' >"$work_root/source-root.key"
.venv/bin/python src/test/acceptance/secret_restore_acceptance.py seed \
  --database-url postgresql+psycopg://deepresearch:deepresearch@localhost:5432/deepresearch \
  --key-file "$work_root/source-root.key" \
  --tenant-id "$tenant_id" --secret-ref "$secret_ref" \
  | tee "$result_root/secret-seed.json"

docker exec deepresearch-mutilagent-postgres-1 \
  pg_dump -Fc --no-owner -U deepresearch deepresearch \
  >"$work_root/deepresearch.dump"
cp "$work_root/source-root.key" "$work_root/recovery/root.key"
rm -f -- "$work_root/source-root.key"
sha256sum "$work_root/deepresearch.dump" "$work_root/recovery/root.key" \
  >"$result_root/backup-checksums.txt"

docker run -d --name "$restore_name" \
  -e POSTGRES_DB=deepresearch \
  -e POSTGRES_USER=deepresearch \
  -e POSTGRES_PASSWORD=deepresearch \
  -p 55434:5432 pgvector/pgvector:pg16 >/dev/null
for _ in {1..60}; do
  docker exec "$restore_name" pg_isready -U deepresearch -d deepresearch \
    >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$restore_name" pg_isready -U deepresearch -d deepresearch >/dev/null
docker cp "$work_root/deepresearch.dump" "$restore_name:/tmp/deepresearch.dump"
docker exec "$restore_name" pg_restore --clean --if-exists --no-owner \
  -U deepresearch -d deepresearch /tmp/deepresearch.dump

export PHASE14_TARGET_DB_HOST=localhost
export PHASE14_TARGET_DB_PORT=55434
export PHASE14_TARGET_DB_NAME=deepresearch
.venv/bin/python src/test/acceptance/secret_restore_acceptance.py verify \
  --database-url postgresql+psycopg://deepresearch:deepresearch@localhost:55434/deepresearch \
  --key-file "$work_root/recovery/root.key" \
  --tenant-id "$tenant_id" --secret-ref "$secret_ref" \
  | tee "$result_root/secret-restore.json"

.venv/bin/python src/test/acceptance/secret_restore_acceptance.py cleanup \
  --database-url postgresql+psycopg://deepresearch:deepresearch@localhost:5432/deepresearch \
  --key-file "$work_root/recovery/root.key" \
  --tenant-id "$tenant_id" --secret-ref "$secret_ref" \
  | tee "$result_root/source-cleanup.json"

printf '%s\n' \
  '{"result":"PASS","backup":"pg_dump","restore":"isolated_postgresql","root_material_scrubbed":true,"test_secret_cleaned_from_source":true}' \
  | tee "$result_root/result.json"

docker rm -f "$restore_name" >/dev/null
rm -rf -- "$work_root"
trap - EXIT
