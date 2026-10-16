#!/bin/bash
# Rust-engine run on the 200 LoCoMo questions; restarts after quota/network stalls (every call is cached).
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
for attempt in $(seq 1 40); do
  SHARD=${SHARD:-} WORKERS=3 PARALLEL_CALLS=3 PAUSE_MUSE=2 PAUSE_HAIKU=2 uv run --no-project --python 3.12 --with fastembed --with psutil \
    --with-editable ../../../hypnos python run_rust.py >> rust.log 2>&1 && { echo "EXIT 0" >> rust.log; exit 0; }
  echo "$(date +%H:%M) attempt $attempt stopped; waiting 10 min" >> rust.log
  sleep 600
done
echo "EXIT 1" >> rust.log
