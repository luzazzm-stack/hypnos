# hypnos (Python package)

The memory engine, local service, MCP server and CLI. See the [repository README](../README.md) for the overview.

## Install

Use the one-line installer (see [INSTALL.md](../INSTALL.md)). By hand (Python 3.10 or newer; the installers use 3.12):

```bash
uv tool install --python 3.12 "./hypnos[mcp,vectors]"     # `hypnos` command on PATH
hypnos setup                                              # connect your coding agents
# or, as a library:
pip install -e ".[all]"                                   # extras: mcp, vectors (fastembed + numpy)
```

## Configure (`~/.hypnos/config.toml`, or `HYPNOS_*` environment variables)

`hypnos setup` writes this for you.

```toml
systemone = "jev"            # jev (hosted, key in ~/.typesafe/credentials or TYPESAFE_API_KEY) | laya (local laya-serve) | llm
systemone_model = ""          # with systemone = "llm": the model that makes the checks, e.g. "claude:haiku" (empty = the memory model)
jev = true                   # use the System One model at all
writer = "auto"              # auto = the writer_chain below; or opencode | claude | codex | openai | fake
writer_chain = ["opencode:opencode/some-free-model", "openai:model@https://api.deepseek.com/v1#DEEPSEEK_API_KEY", "claude:haiku"]
writer_parallel = 0          # 0 = automatic (1 under 12 GB RAM, else 3)
vectors = true               # local bge-small meaning search
stay_running = false         # false = stop ~90 s after the last agent closes; true keeps it on (servers, robots)
port = 7439
```

## Python API

```python
from hypnos import Memory

mem = Memory(space="project-x", profile="coding")   # profiles: coding, chat, search, browsing, robotics, generic

mem.log(text, role="user|agent|tool|observation", session="s1", meta={"ts": ..., "evidence": [...]})
mem.recall(query, budget=6000)   # -> Recall(context, exact, level, tokens, topics, trace)
mem.wake()                       # starting context for a new session
mem.sleep()                      # consolidate everything after the watermark
mem.pin(claim)                   # user fact, highest trust after CLAUDE.md
mem.correct(fact_id, claim)      # replace a fact, keep history
mem.forget(query=..., hard=True) # archive facts; hard=True also deletes the source text
mem.export()                     # readable markdown of the memory
mem.status()
```

## CLI

```
hypnos serve                        local service (127.0.0.1:7439)
hypnos mcp                          MCP server over stdio
hypnos hook claude <Event>          Claude Code hook handler
hypnos hook generic <event>         any other agent with hooks
hypnos setup [--yes] [--systemone jev|laya|none] [--list-models] [--memory-model ...] [--uninstall]
hypnos cleanup [--apply "1-5, T2"] [--space NAME]   the /memory-cleanup list: every fact numbered under its topic
hypnos chat-proxy --upstream URL    memory for any OpenAI-compatible chat client
hypnos log|recall|sleep|pin|forget|status|export|wake --space S --profile P
```

## Service API (JSON over HTTP, 127.0.0.1 only)

`POST /log /recall /wake /sleep /pin /correct /forget /forget_ids /purge /pause /review /cleanup_view /cleanup_apply /status /export /ping`. Every call takes `space` and `profile`.

The service also sleeps a session automatically when a new session starts, after 10 idle minutes, or once 150 unconsolidated log rows pile up.

## Tests

```bash
uv run --python 3.12 --with pytest --with-editable . python -m pytest -q tests     # offline, no keys
```
