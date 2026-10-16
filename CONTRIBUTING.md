# Contributing

## Setup

```bash
cd hypnos
uv run --python 3.12 --with pytest --with-editable . python -m pytest -q tests
```

Hypnos supports Python 3.10 and newer; the installers use 3.12. The tests run fully offline. They use `FakeWriter` (a deterministic writer with no LLM) and no Jev, so they need no keys and no network.

## Ground rules

- **Follow the architecture rules R1–R15** in [docs/architecture.html](docs/architecture.html). A change that breaks one is a design change: update the spec in the same pull request.
- **Memory must never break an agent.** Hooks, plugins and proxies swallow errors and time out quickly.
- **Nothing leaves the machine unredacted.** New logging paths must go through `Memory.log` (the redactor).
- **Every typed decision needs a plain-code fallback,** so the engine works without Jev.
- **Benchmarks:** report what was measured, on which questions, graded by which model, and say when numbers are in-sample.

## Layout

| Path | What |
|---|---|
| `hypnos/src/hypnos/engine.py` | log, sleep, recall, wake, pin/correct/forget |
| `hypnos/src/hypnos/store.py` | SQLite schema, FTS5 search |
| `hypnos/src/hypnos/profiles.py` | per-agent fact kinds and staleness |
| `hypnos/src/hypnos/jev.py`, `llm.py` | System One model (Jev, Laya, or the LLM) and the writer models |
| `hypnos/src/hypnos/config.py`, `setup_wizard.py` | `~/.hypnos/config.toml` and `hypnos setup` |
| `hypnos/src/hypnos/daemon.py`, `client.py`, `hooks.py`, `mcp_server.py`, `cli.py` | service and entry points |
| `hypnos/src/hypnos/integrations/` | chat proxy and agent adapters |
