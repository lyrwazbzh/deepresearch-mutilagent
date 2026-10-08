#!/usr/bin/env bash
set -euo pipefail

: "${PG_BACKUP_URL:?libpq PostgreSQL backup URL is required}"
: "${MINIO_ENDPOINT:?MINIO_ENDPOINT is required}"
: "${MINIO_ACCESS_KEY:?MINIO_ACCESS_KEY is required}"
: "${MINIO_SECRET_KEY:?MINIO_SECRET_KEY is required}"
: "${SECRET_RECOVERY_SOURCE:?SECRET_RECOVERY_SOURCE is required}"

backup_root=${1:?usage: backup.sh BACKUP_ROOT}
interval_hours=${BACKUP_INTERVAL_HOURS:-6}
if (( interval_hours < 1 || interval_hours > 6 )); then
  echo "BACKUP_INTERVAL_HOURS must be between 1 and 6" >&2
  exit 2
fi
if [[ ! -e "$SECRET_RECOVERY_SOURCE" ]]; then
  echo "SECRET_RECOVERY_SOURCE does not exist" >&2
  exit 2
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
destination="$backup_root/$stamp"
incremental="$backup_root/incremental"
mkdir -p "$destination/postgres/base" "$destination/minio" \
  "$destination/secrets" "$incremental/minio/documents" \
  "$incremental/minio/workspaces"

started=$(date -u +%s)
pg_dump --format=custom --no-owner --file="$destination/postgres/deepresearch.dump" \
  "$PG_BACKUP_URL"
pg_basebackup --dbname="$PG_BACKUP_URL" --pgdata="$destination/postgres/base" \
  --format=tar --gzip --wal-method=stream --checkpoint=fast

mc alias set backup-source "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" \
  "$MINIO_SECRET_KEY" >/dev/null
mc mirror --overwrite --remove backup-source/documents "$incremental/minio/documents"
mc mirror --overwrite --remove backup-source/workspaces "$incremental/minio/workspaces"
cp -al "$incremental/minio/documents" "$destination/minio/documents"
cp -al "$incremental/minio/workspaces" "$destination/minio/workspaces"
mc ls --recursive --json backup-source/documents > "$destination/minio/documents.inventory.ndjson"
mc ls --recursive --json backup-source/workspaces > "$destination/minio/workspaces.inventory.ndjson"

tar --create --gzip --file="$destination/secrets/recovery-material.tar.gz" \
  --directory="$(dirname "$SECRET_RECOVERY_SOURCE")" \
  "$(basename "$SECRET_RECOVERY_SOURCE")"

(
  cd "$destination"
  find postgres minio secrets -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
)
completed=$(date -u +%s)
cat > "$destination/manifest.json" <<EOF
{"schema_version":1,"created_at":"$stamp","completed_in_seconds":$((completed-started)),"maximum_rpo_hours":$interval_hours,"postgresql":["logical_dump","physical_base_backup","streamed_wal"],"minio":"incremental_mirror_with_inventory","authorities":["postgresql","minio","kms_secret_recovery_material"],"derived_stores":["redis_empty","elasticsearch_rebuild","neo4j_rebuild"]}
EOF
echo "$destination"
