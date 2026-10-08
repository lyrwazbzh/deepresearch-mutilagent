#!/usr/bin/env bash
set -euo pipefail

source_path=${1:?WAL source path is required}
wal_name=${2:?WAL segment name is required}
: "${WAL_ARCHIVE_DIRECTORY:?WAL_ARCHIVE_DIRECTORY is required}"
mkdir -p "$WAL_ARCHIVE_DIRECTORY"
install -m 0600 "$source_path" "$WAL_ARCHIVE_DIRECTORY/$wal_name"
