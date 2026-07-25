#!/usr/bin/env bash
set -euo pipefail

BUCKET="${BUCKET:?BUCKET env var must be set}"
DATA_ROOT="${DATA_ROOT:-/opt/dlami/nvme/hf-cache/data}"
SPLIT="finevideo/sports"

mkdir -p "${DATA_ROOT}/${SPLIT}"

# manifest + 각 영상의 frames/ , inference/ 전부 로컬로
s5cmd --numworkers 32 sync "s3://${BUCKET}/${SPLIT}/*" "${DATA_ROOT}/${SPLIT}/"

echo "synced to ${DATA_ROOT}/${SPLIT}"
