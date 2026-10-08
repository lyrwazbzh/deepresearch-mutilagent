#!/usr/bin/env bash
set -euo pipefail

: "${ALLOW_DESTRUCTIVE_RESTORE:?must equal YES_I_AM_USING_AN_ISOLATED_TARGET}"
if [[ "$ALLOW_DESTRUCTIVE_RESTORE" != "YES_I_AM_USING_AN_ISOLATED_TARGET" ]]; then
  echo "PITR refused: target was not explicitly declared isolated" >&2
  exit 2
fi
base_directory=${1:?base-backup directory is required}
wal_directory=${2:?WAL archive directory is required}
target_pgdata=${3:?clean target PGDATA is required}
if [[ -e "$target_pgdata/PG_VERSION" ]]; then
  echo "PITR target must be a clean PGDATA directory" >&2
  exit 2
fi
mkdir -p "$target_pgdata"
tar --extract --gzip --file="$base_directory/base.tar.gz" --directory="$target_pgdata"
if [[ -f "$base_directory/pg_wal.tar.gz" ]]; then
  mkdir -p "$target_pgdata/pg_wal"
  tar --extract --gzip --file="$base_directory/pg_wal.tar.gz" \
    --directory="$target_pgdata/pg_wal"
fi
cat >> "$target_pgdata/postgresql.auto.conf" <<EOF
restore_command = 'cp $wal_directory/%f %p'
recovery_target_action = 'promote'
EOF
touch "$target_pgdata/recovery.signal"
echo "PITR files prepared in $target_pgdata; start the matching PostgreSQL major version."
