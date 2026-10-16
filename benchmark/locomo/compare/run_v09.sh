#!/bin/bash
# v0.9 vs v0.8 on LoCoMo, on the a Linux machine. Retries every 10 minutes (Muse free quota); every model call is cached.
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH" PYTHONIOENCODING=utf-8 WORKERS=1 PARALLEL_CALLS=1 PAUSE_MUSE=2 PAUSE_HAIKU=2 TAG=${TAG:-v09}
cd "$(dirname "$0")"
for i in $(seq 1 150); do
  uv run --no-project --python 3.12 --with fastembed --with-editable ../../../hypnos python run_v09.py >> $TAG.log 2>&1 && break
  echo "$(date +%H:%M) stopped; waiting 10 min" >> $TAG.log; sleep 600
done
echo EXIT >> $TAG.log
