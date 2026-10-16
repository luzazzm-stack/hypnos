#!/bin/bash
# LoCoMo answered by Claude Opus 5.5 (medium); restarts after stalls. Every model call is cached, so restarts resume.
cd "$(dirname "$0")"
export PATH="$PATH:$HOME/.bun/bin" PYTHONIOENCODING=utf-8 MEM0_TELEMETRY=False
SM=https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
curl -s -m 3 http://127.0.0.1:8080/health >/dev/null || (uv run --no-project --python 3.12 --with fastembed python embed_server.py >> embed_server.log 2>&1 &)
sleep 15
for attempt in $(seq 1 20); do
  WORKERS=3 PARALLEL_CALLS=3 PAUSE_MUSE=1 PAUSE_HAIKU=1 uv run --no-project --python 3.12 --with "mem0ai[nlp]" --with fastembed \
    --with "en_core_web_sm @ $SM" --with-editable ../../../hypnos python run_opus.py >> opus.log 2>&1 && { echo "EXIT 0" >> opus.log; exit 0; }
  echo "$(date +%H:%M) attempt $attempt stopped; waiting 5 min" >> opus.log
  sleep 300
done
echo "EXIT 1" >> opus.log
