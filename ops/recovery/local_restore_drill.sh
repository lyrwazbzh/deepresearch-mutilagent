#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
artifact_root=${PHASE14_DRILL_ROOT:?PHASE14_DRILL_ROOT must be a new persistent directory}
if [[ -e "$artifact_root" ]]; then
  echo "Refusing to reuse restore drill directory: $artifact_root" >&2
  exit 2
fi
mkdir -p "$artifact_root"/{postgres,minio,secrets,restored-secrets,logs}

restore_containers=(
  phase14-restore-pg-a
  phase14-restore-minio-a
  phase14-restore-redis-a
  phase14-restore-es-a
  phase14-restore-neo4j-a
)
for container_name in "${restore_containers[@]}"; do
  if docker container inspect "$container_name" >/dev/null 2>&1; then
    echo "Refusing to replace existing container: $container_name" >&2
    exit 2
  fi
done

worker_pid=""
dispatcher_pid=""
cleanup() {
  if [[ -n "$worker_pid" ]]; then kill "$worker_pid" 2>/dev/null || true; fi
  if [[ -n "$dispatcher_pid" ]]; then kill "$dispatcher_pid" 2>/dev/null || true; fi
  docker rm -f "${restore_containers[@]}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$root"
docker compose --profile task-kg up -d --wait

export DATABASE_URL=postgresql+psycopg://deepresearch:deepresearch@localhost:5432/deepresearch
export REDIS_URL=redis://localhost:6379/0
export CELERY_BROKER_URL=redis://localhost:6379/1
export DEEPRESEARCH_MODEL_MODE=fake
export PHASE10_AUTH_ENABLED=0
export DEFAULT_TENANT_ID=tenant_phase14_real
export DEFAULT_USER_ID=user_phase14_real
export MINIO_ENDPOINT=localhost:9000
export MINIO_ACCESS_KEY=minioadmin
export MINIO_SECRET_KEY=minioadmin
export MINIO_SECURE=0
export ELASTICSEARCH_URL=http://localhost:9200
export NEO4J_URL=http://localhost:7474
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=deepresearch
export PHASE14_ACCEPTANCE_SECRET_OUTPUT="$artifact_root/share-secret.json"
export PHASE14_SHARE_SECRET_FILE="$artifact_root/share-secret.json"
export PHASE14_RECOVERY_SEED_OUTPUT="$artifact_root/recovery-seed.json"
export PHASE14_RECOVERY_FIXTURE_OUTPUT="$artifact_root/recovery-fixture.json"

.venv/bin/celery -A src.agent.workers.celery_app:celery_app worker \
  -Q export --concurrency=2 --prefetch-multiplier=1 --loglevel=WARNING \
  >"$artifact_root/logs/source-export-worker.log" 2>&1 &
worker_pid=$!
.venv/bin/python -m src.agent.workers.dispatcher_main \
  >"$artifact_root/logs/source-dispatcher.log" 2>&1 &
dispatcher_pid=$!
.venv/bin/python src/test/acceptance/share_export_acceptance.py
kill "$worker_pid" "$dispatcher_pid" 2>/dev/null || true
wait "$worker_pid" "$dispatcher_pid" 2>/dev/null || true
worker_pid=""
dispatcher_pid=""

PHASE14_RECOVERY_FIXTURE_MODE=seed \
  .venv/bin/python src/test/fixtures/recovery_fixture.py
.venv/bin/python src/test/fixtures/seed_recovery.py

date +%s >"$artifact_root/backup-started.epoch"
docker exec deepresearch-mutilagent-postgres-1 \
  pg_dump -Fc --no-owner -U deepresearch deepresearch \
  >"$artifact_root/postgres/deepresearch.dump"
docker exec -e PGPASSWORD=deepresearch deepresearch-mutilagent-postgres-1 \
  pg_basebackup -h localhost -U deepresearch \
  -D /tmp/phase14_base_20261008c -Ft -z -X stream -c fast
docker cp deepresearch-mutilagent-postgres-1:/tmp/phase14_base_20261008c/. \
  "$artifact_root/postgres/base"
SOURCE_MINIO_ENDPOINT=localhost:9000 \
SOURCE_MINIO_ACCESS_KEY=minioadmin \
SOURCE_MINIO_SECRET_KEY=minioadmin \
  .venv/bin/python ops/recovery/minio_transfer.py backup "$artifact_root/minio"
SECRET_RECOVERY_CHECK_VALUE=phase14-real-secret-check \
  .venv/bin/python ops/recovery/create_secret_recovery_check.py "$artifact_root/secrets"
(
  cd "$artifact_root"
  find postgres minio secrets -type f -print0 \
    | sort -z | xargs -0 sha256sum >SHA256SUMS
  sha256sum --check SHA256SUMS >logs/backup-checksums.log
)
date +%s >"$artifact_root/backup-completed.epoch"

docker run -d --name phase14-restore-pg-a \
  -e POSTGRES_DB=deepresearch -e POSTGRES_USER=deepresearch \
  -e POSTGRES_PASSWORD=deepresearch -p 55432:5432 pgvector/pgvector:pg16 >/dev/null
docker run -d --name phase14-restore-minio-a \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  -p 19000:9000 -p 19001:9001 \
  minio/minio:RELEASE.2025-02-07T23-21-09Z \
  /minio server /data --console-address :9001 >/dev/null
docker run -d --name phase14-restore-redis-a -p 16379:6379 redis:7-alpine >/dev/null
docker run -d --name phase14-restore-es-a \
  -e discovery.type=single-node -e xpack.security.enabled=false \
  -e ES_JAVA_OPTS=-Xms512m\ -Xmx512m -p 19200:9200 \
  docker.elastic.co/elasticsearch/elasticsearch:8.17.1 >/dev/null
docker run -d --name phase14-restore-neo4j-a \
  -e NEO4J_AUTH=neo4j/deepresearch -p 17474:7474 -p 17687:7687 \
  neo4j:5.26-community >/dev/null

for _ in {1..60}; do
  docker exec phase14-restore-pg-a pg_isready -U deepresearch -d deepresearch \
    >/dev/null 2>&1 && break
  sleep 1
done
docker exec phase14-restore-pg-a pg_isready -U deepresearch -d deepresearch
for _ in {1..60}; do
  curl -fsS http://localhost:19000/minio/health/ready >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://localhost:19000/minio/health/ready >/dev/null
for _ in {1..90}; do
  curl -fsS http://localhost:19200/_cluster/health >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://localhost:19200/_cluster/health >/dev/null
for _ in {1..90}; do
  docker exec phase14-restore-neo4j-a cypher-shell \
    -u neo4j -p deepresearch 'RETURN 1' >/dev/null 2>&1 && break
  sleep 1
done
docker exec phase14-restore-neo4j-a cypher-shell \
  -u neo4j -p deepresearch 'RETURN 1' >/dev/null

date +%s >"$artifact_root/restore-started.epoch"
cp -a "$artifact_root/secrets/." "$artifact_root/restored-secrets/"
.venv/bin/python ops/recovery/verify_secret_recovery.py "$artifact_root/restored-secrets"
docker cp "$artifact_root/postgres/deepresearch.dump" \
  phase14-restore-pg-a:/tmp/deepresearch.dump
docker exec phase14-restore-pg-a pg_restore --clean --if-exists --no-owner \
  -U deepresearch -d deepresearch /tmp/deepresearch.dump
RESTORE_MINIO_ENDPOINT=localhost:19000 \
RESTORE_MINIO_ACCESS_KEY=minioadmin \
RESTORE_MINIO_SECRET_KEY=minioadmin \
  .venv/bin/python ops/recovery/minio_transfer.py restore "$artifact_root/minio"
docker exec phase14-restore-redis-a redis-cli FLUSHALL >/dev/null
test "$(docker exec phase14-restore-redis-a redis-cli DBSIZE)" = "0"

export DATABASE_URL=postgresql+psycopg://deepresearch:deepresearch@localhost:55432/deepresearch
export REDIS_URL=redis://localhost:16379/0
export CELERY_BROKER_URL=redis://localhost:16379/1
export MINIO_ENDPOINT=localhost:19000
export ELASTICSEARCH_URL=http://localhost:19200
export NEO4J_URL=http://localhost:17474
RESTORE_DATABASE_URL="$DATABASE_URL" \
RESTORE_MINIO_ENDPOINT=localhost:19000 \
RESTORE_MINIO_ACCESS_KEY=minioadmin \
RESTORE_MINIO_SECRET_KEY=minioadmin \
RESTORE_SECRET_DIR="$artifact_root/restored-secrets" \
STRICT_RESTORE_INTEGRITY=0 \
  .venv/bin/python -m ops.recovery.verify_restore \
  | tee "$artifact_root/logs/restore-integrity.json"
PHASE14_RECOVERY_FIXTURE_MODE=rebuild \
  .venv/bin/python src/test/fixtures/recovery_fixture.py \
  | tee "$artifact_root/logs/derived-rebuild.json"

.venv/bin/celery -A src.agent.workers.celery_app:celery_app worker \
  -Q export --concurrency=1 --prefetch-multiplier=1 --loglevel=WARNING \
  >"$artifact_root/logs/restored-export-worker.log" 2>&1 &
worker_pid=$!
.venv/bin/python -m src.agent.workers.dispatcher_main \
  >"$artifact_root/logs/restored-dispatcher.log" 2>&1 &
dispatcher_pid=$!
.venv/bin/python src/test/acceptance/restored_application_acceptance.py \
  | tee "$artifact_root/logs/restored-application.json"
kill "$worker_pid" "$dispatcher_pid" 2>/dev/null || true
wait "$worker_pid" "$dispatcher_pid" 2>/dev/null || true
worker_pid=""
dispatcher_pid=""
date +%s >"$artifact_root/restore-completed.epoch"

.venv/bin/python - "$artifact_root" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
backup = int((root / "backup-completed.epoch").read_text()) - int(
    (root / "backup-started.epoch").read_text()
)
restore = int((root / "restore-completed.epoch").read_text()) - int(
    (root / "restore-started.epoch").read_text()
)
integrity = json.loads((root / "logs/restore-integrity.json").read_text())
complete = integrity["missing_count"] == 0 and integrity["mismatched_count"] == 0
result = {
    "result": "PASS" if complete else "PARTIAL",
    "maximum_rpo_hours": 6,
    "backup_elapsed_seconds": backup,
    "core_and_application_restore_elapsed_seconds": restore,
    "core_rto_target_seconds": 28800,
    "full_rto_target_seconds": 86400,
    "redis": "empty_then_pg_outbox_recovered",
    "elasticsearch": "rebuilt_from_pg_minio",
    "neo4j": "rebuilt_from_task_evidence",
    "source_integrity": integrity,
}
(root / "restore-drill-result.json").write_text(json.dumps(result, sort_keys=True))
print(json.dumps(result, sort_keys=True))
PY
