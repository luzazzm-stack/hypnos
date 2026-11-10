# agentmem (Rust engine): frozen for the future

> **Status: frozen at architecture v0.8 (29 September 2026), not maintained.** Python (`hypnos/`, formerly `agentmem/`) is the main engine;
> design changes are made and tested there first. This Rust version is kept for later, for example to ship agentmem as one
> self-contained program. To use it anyway: build it, put it on the PATH as `agentmem-rs` (or set `AGENTMEM_RUST_EXE`) and set `HYPNOS_ENGINE=rust`.


The agentmem memory engine (architecture v0.8) in Rust: one native program for storage, search, Jev-routed recall,
sleep consolidation and the local memory service.

It worked unchanged with the v0.8 integrations. Later Python-only features (cleanup, review, pause, shared rules, the agent check-in) are not in it:

- **Same database:** the SQLite schema, full-text indexes and vector format are identical, so existing memories open as they are.
- **Same service API:** HTTP on `127.0.0.1:7439` (`/log`, `/recall`, `/wake`, `/sleep`, `/pin`, `/correct`, `/forget`, `/status`, `/export`, `/ping`).
  The Claude Code hooks, the opencode plugin and the MCP server talk to it unchanged.
- **Same prompts and Jev questions,** so it builds and recalls the same memories.

## Build and install

Needs Rust (stable) and, on Windows, the Microsoft C++ build tools.

```
cargo build --release
copy target\release\agentmem.exe %USERPROFILE%\.local\bin\agentmem-rs.exe
```

With `HYPNOS_ENGINE=rust` set and `agentmem-rs` on the PATH, the Python client (`hypnos.client.ensure_running`) starts it
instead of the Python service. Without it, the Python engine runs (the default).

## Use

```
agentmem-rs serve                                   # the local memory service
agentmem-rs recall "which port does the API use?" --space code-myrepo --vectors
agentmem-rs log "we moved the API to port 8443" --space code-myrepo
agentmem-rs sleep --space code-myrepo
agentmem-rs status | wake | export --space code-myrepo
agentmem-rs index-log --space code-myrepo --vectors  # meaning search over raw messages (older memories)
```

Settings: the Rust engine still reads the pre-rename `~/.agentmem/config.toml` and `AGENTMEM_*` environment variables; the Python engine has moved to `~/.hypnos` and `HYPNOS_*`.
Local meaning search uses bge-small-en-v1.5 (ONNX, CPU) and is built in by default (`--no-default-features` builds without it).

## Full LoCoMo benchmark, Rust vs Python (200 questions, 29 September 2026)

| | Rust | Python |
|---|---|---|
| Accuracy | 55% | 55% |
| Context per question | ~530 tokens | ~600 tokens |
| Search time, warm (median) | 3.0 s | 3.8 s |
| Search time, cold start | ~3.9 s | ~7.8 s |

## First parity check (29 September 2026)

| | Rust | Python |
|---|---|---|
| Correct answer in the recalled context (20 LoCoMo questions) | 17 / 20 | 17 / 20 (same questions) |
| Average context sent | ~680 tokens | ~750 tokens |
| One search, cold start (process start + model load + Jev) | ~3.9 s | ~7.8 s |
| Peak RAM, one search | ~260 MB | ~235 MB |

Most of the time is Jev's network round trips and most of the RAM is the embedding model, which is the same size in both languages.

© 2026 Luza Maharjan. MIT License.
