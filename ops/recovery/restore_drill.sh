#!/usr/bin/env bash
set -euo pipefail

: "${ALLOW_DESTRUCTIVE_RESTORE:?must equal YES_I_AM_USING_AN_ISOLATED_TARGET}"
if [[ "$ALLOW_DESTRUCTIVE_RESTORE" != "YES_I_AM_USING_AN_ISOLATED_TARGET" ]]; then
  echo "Restore refused: target was not explicitly declared isolated" >&2
  exit 2
fi
: "${RESTORE_DATABASE_URL:?required}"
: "${RESTORE_MINIO_ENDPOINT:?required}"
: "${RESTORE_MINIO_ACCESS_KEY:?required}"
: "${RESTORE_MINIO_SECRET_KEY:?required}"
: "${RESTORE_REDIS_URL:?required}"
: "${RESTORE_SECRET_DIR:?required}"

backup_dir=${1:?usage: restore_drill.sh BACKUP_DIRECTORY}
if [[ ! -f "$backup_dir/manifest.json" || ! -f "$backup_dir/SHA256SUMS" ]]; then
  echo "Backup manifest or checksums are missing" >&2
  exit 2
fi

started=$(date -u +%s)
(
  cd "$backup_dir"
  sha256sum --check SHA256SUMS
)

mkdir -p "$RESTORE_SECRET_DIR"
tar --extract --gzip \
  --file="$backup_dir/secrets/recovery-material.tar.gz" \
  --directory="$RESTORE_SECRET_DIR"
python ops/recovery/verify_secret_recovery.py "$RESTORE_SECRET_DIR"

pg_restore --clean --if-exists --no-owner --dbname="$RESTORE_DATABASE_URL" \
  "$backup_dir/postgres/deepresearch.dump"

mc alias set restore-target "$RESTORE_MINIO_ENDPOINT" "$RESTORE_MINIO_ACCESS_KEY" \
  "$RESTORE_MINIO_SECRET_KEY" >/dev/null
mc mb --ignore-existing restore-target/documents
mc mb --ignore-existing restore-target/workspaces
mc mirror --overwrite "$backup_dir/minio/documents" restore-target/documents
mc mirror --overwrite "$backup_dir/minio/workspaces" restore-target/workspaces

redis-cli -u "$RESTORE_REDIS_URL" FLUSHALL >/dev/null
if [[ "$(redis-cli -u "$RESTORE_REDIS_URL" DBSIZE)" != "0" ]]; then
  echo "The isolated Redis target is not empty after restore initialization" >&2
  exit 1
fi

python -m ops.recovery.verify_restore
completed=$(date -u +%s)
elapsed=$((completed-started))
cat > "$backup_dir/restore-drill-result.json" <<EOF
{"schema_version":1,"completed_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","elapsed_seconds":$elapsed,"core_rto_target_seconds":28800,"full_rto_target_seconds":86400,"postgresql":"verified","minio":"verified","kms_secret":"verified","redis":"empty","elasticsearch":"rebuild_required","neo4j":"rebuild_required"}
EOF
echo "Restore drill PASS in ${elapsed}s; ES and Neo4j remain derived-store rebuild steps."
