# How the architecture runs on opencode

Every model call in this benchmark goes through **stock opencode** (`opencode run`) with the free model `opencode/muse-spark-1.3-contributor-free`. This file explains that setup so it can be reproduced.

## Isolated config

`cfg/opencode/opencode.jsonc` is an empty, stock opencode config:

```jsonc
{ "$schema": "https://opencode.ai/config.json" }
```

The scripts point opencode at it with `XDG_CONFIG_HOME=<benchmark>/cfg`, so your normal `~/.config/opencode` (agents, plugins, MCP servers) is never loaded.

**Why stock:** OpenCode's free tier only answers requests that look like genuine OpenCode. A custom agent, custom permissions, or calling the `opencode serve` HTTP API directly all returned `403 FreeTierError: "OpenCode's free tier can only be used from within OpenCode"`.

## How each call is made

`bench.py` → `muse()`:

1. Large inputs (session logs, memories, candidate answers) are written to a file and attached with `-f <file>`, because Windows command lines max out around 32k characters.
2. The prompt starts with *"Do not use any tools. Answer directly from the attached file(s)"*, because the stock `build` agent has tools.
3. `--format json` streams events; the answer is the `text` events, token counts come from `step_finish` (`tokens.input`, `tokens.output`, `tokens.reasoning`, `tokens.cache.read`).
4. Every stock call carries about **10,900 input tokens** of opencode's own system prompt and tool list; the reported per-call input subtracts this (`OVERHEAD_IN`).
5. Results are cached in `cache/` by a hash of the prompt and attachments.

```bash
XDG_CONFIG_HOME=benchmark/cfg opencode run -m opencode/muse-spark-1.3-contributor-free \
  --format json -f attached.txt -- "Do not use any tools. ... <prompt>"
```

## Plain opencode baseline (`bench_g.py`)

The "no architecture" comparison gives stock opencode a folder of the 15 session logs (`run_plain/logs/`, the same text as `session-logs/`) and lets its `build` agent search them with its normal tools (grep, glob, read). It averaged ~7 steps and ~35,000 input tokens per question, excluding the system prompt at each step.

## Limits met

- **Free quota:** after roughly 650 calls in a day, every request returns `Rate limit exceeded`. It appears to reset daily at 00:00 UTC. `run_h_when_ready.sh` waits for it and then starts the run.
- **Muse Spark 1.2** (used as the cross-check grader) returned `Unexpected server error` for a day; `NO_GRADER2=1` skips it.
- **"database is locked":** two parallel `opencode run` processes writing opencode's local database at once. Harmless; the harness retries. `WORKERS=2` keeps it rare.
- If a request is rate-limited, `opencode run` retries internally and never returns; the harness times each call out and retries.
