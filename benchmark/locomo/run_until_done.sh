#!/bin/bash
# Runs the LoCoMo benchmark slowly until it finishes. Everything is cached, so every restart resumes.
# - waits 15 min between attempts (free-quota limits)
# - watchdog: if no model call finishes for 20 min (e.g. a connection hung during a power or internet cut), restart
cd "$(dirname "$0")"
restore_sleep() { powershell -NoProfile -Command "powercfg /change standby-timeout-dc 15; powercfg /change hibernate-timeout-dc 15" >/dev/null 2>&1; echo "$(date +%H:%M) battery sleep restored to 15 min" >> run.log; }
newest() { ls -At cache 2>/dev/null | head -1 | xargs -I{} stat -c %Y "cache/{}" 2>/dev/null || date +%s; }
for attempt in $(seq 1 48); do
  touch "cache/.watchdog"
  PYTHONIOENCODING=utf-8 WORKERS=4 PARALLEL_CALLS=3 PAUSE_MUSE=2 PAUSE_HAIKU=2 FULL_CONTEXT=0 uv run --no-project --python 3.12 --with fastembed --with-editable ../../hypnos \
    python run_locomo.py >> run.log 2>&1 &
  pid=$!
  while kill -0 $pid 2>/dev/null; do
    sleep 30
    if [ $(( $(date +%s) - $(newest) )) -gt 600 ]; then
      echo "$(date +%H:%M) watchdog: no progress for 20 min, restarting" >> run.log
      powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*run_locomo.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" >/dev/null 2>&1
      kill $pid 2>/dev/null
      touch "cache/.watchdog"
    fi
  done
  wait $pid && { echo "EXIT 0" >> run.log; restore_sleep; exit 0; }
  echo "$(date +%H:%M) attempt $attempt stopped; waiting 15 min" >> run.log
  for i in $(seq 1 10); do sleep 30; done
  touch "cache/.watchdog"
done
echo "EXIT 1" >> run.log; restore_sleep
