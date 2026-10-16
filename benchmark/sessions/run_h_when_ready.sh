#!/bin/bash
# Waits for the Muse Spark free quota to come back, then runs the v0.6 benchmark.
cd ~/memtest
while true; do
  r=$(cd run && XDG_CONFIG_HOME=~/memtest/cfg timeout 90 opencode run -m opencode/muse-spark-1.3-contributor-free --format json "Reply with exactly: PONG" 2>&1 | grep -c '"text":"PONG"')
  if [ "$r" -ge 1 ]; then echo "$(date +%H:%M) quota back, starting v0.6"; break; fi
  echo "$(date +%H:%M) quota still exhausted, next check in 15 min"
  powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='opencode.exe'\" | Where-Object { \$_.CommandLine -like '*PONG*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" >/dev/null 2>&1
  for i in $(seq 1 30); do sleep 30; done   # short sleeps survive laptop standby better
done
WORKERS=2 PYTHONIOENCODING=utf-8 uv run --no-project --python 3.12 --with fastembed python bench_h.py > h.log 2>&1
echo "EXIT $?" >> h.log
