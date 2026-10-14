# hypnos for Claude Code

Two parts, both optional and independent:

| Part | What it does |
|---|---|
| **Hooks** | Automatic memory: wake context at session start, relevant memory injected with each prompt, every tool call logged, sleep on compaction and session end. |
| **MCP server** | Tools Claude can call on purpose: `memory_recall`, `memory_remember` (optionally for every project), `memory_correct`, `memory_forget`, `memory_review`, `memory_cleanup_suggestions`, `memory_forget_ids`, `memory_purge`, `memory_pause`, `memory_sleep`, `memory_status`. |
| **`/memory-cleanup`** | A slash command: shows duplicates, unused and stale facts, lets you pick what to delete, keep or correct. Copy `commands/memory-cleanup.md` to `~/.claude/commands/`. |

## Install

```bash
# 1. install the hypnos command (puts hypnos on PATH)
uv tool install --editable ./hypnos --with mcp

# 2. MCP server, for all projects
claude mcp add --scope user hypnos -- hypnos mcp

# 3. hooks: merge settings.hooks.json into ~/.claude/settings.json (keep your existing hooks)
```

## How it maps to the architecture

| Hook | Architecture step |
|---|---|
| `SessionStart` | **wake**: pinned user facts, topic small summaries, wake note |
| `UserPromptSubmit` | log the prompt (redacted), **G1** "needs memory?", then **v0.8 recall** (Jev-routed: the smallest context that answers); the result is added as context |
| `PostToolUse` | log the tool call; file tools get `code:path@commit` evidence, so a fact goes **stale** when git shows the file changed (R13) |
| `Stop` | log the assistant's final reply |
| `PreCompact` | mark the compaction (older turns of this session may be recalled again), then **sleep** |
| `SessionEnd` | **sleep** this session in the background |

- **Memory space:** one per git repository, `code-<repo name>`. Override it with `HYPNOS_SPACE`.
- **No echo:** recall skips messages of the current conversation that are still in view, so the agent never gets its own chat back as "memory".
- **Sleep timing:** on compaction, at session end, when a new session starts, after 10 idle minutes, or once 150 new rows pile up.
- **Safety:** hooks never block or fail a session. If the service is down they exit silently.
- **No recursion:** when hypnos uses `claude -p` as its writer, it sets `HYPNOS_DISABLE=1`, so those runs don't trigger the hooks again.
