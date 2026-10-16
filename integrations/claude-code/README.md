# hypnos for Claude Code

Three parts, all optional and independent:

| Part | What it does |
|---|---|
| **Hooks** | Automatic memory: wake context at session start, relevant memory injected with each prompt, every tool call logged, sleep on compaction and session end. |
| **MCP server** | Tools Claude can call on purpose: `memory_recall`, `memory_remember` (optionally for every project), `memory_note`, `memory_correct`, `memory_forget`, `memory_review`, `memory_cleanup_view`, `memory_cleanup_apply`, `memory_cleanup_suggestions`, `memory_forget_ids`, `memory_purge`, `memory_pause`, `memory_sleep`, `memory_status`. |
| **`/memory-cleanup`** | A slash command: lists every fact numbered under its topic (T1, T2...), newest first (up to 150; "show T3" lists a topic's older ones), plus numbered "Duplicates (the older copy goes)", "Unused for a month" and "Stale" sections. Delete by number, range or topic ("1-5, T2"), "all duplicates", "all unused", "all stale", or "purge" (permanently delete forgotten facts). Copy `commands/memory-cleanup.md` to `~/.claude/commands/`. |

## Install

`hypnos setup` does all three for you (see [INSTALL.md](../../INSTALL.md)). By hand:

```bash
# 1. install the hypnos command (puts hypnos on PATH)
uv tool install --python 3.12 "./hypnos[mcp,vectors]"

# 2. MCP server, for all projects
claude mcp add --scope user hypnos -- hypnos mcp

# 3. hooks: merge settings.hooks.json into ~/.claude/settings.json (keep your existing hooks)
```

## How it maps to the architecture

| Hook | Architecture step |
|---|---|
| `SessionStart` | **wake**: pinned user facts, topic small summaries, wake note |
| `UserPromptSubmit` | log the prompt (redacted), **G1** "needs memory?", then **v0.8 recall** (System One-routed: the smallest context that answers); the result is added as context |
| `PostToolUse` | log the tool call; file tools get `code:path@commit` evidence, so a fact goes **stale** when git shows the file changed (R13) |
| `Stop` | log the assistant's final reply |
| `PreCompact` | mark the compaction (older turns of this session may be recalled again), then **sleep** |
| `SessionEnd` | **sleep** this session in the background |

- **Memory space:** `code-<git repo or folder name>`, from the folder the session started in. A session keeps that one memory even if it later changes folder (pinned by session id in `~/.hypnos/session-spaces/`). Override it with `HYPNOS_SPACE`.
- **No echo:** recall skips messages of the current conversation that are still in view, so the agent never gets its own chat back as "memory". Hypnos never stores its own output (cleanup lists, wake notes, recalled memory).
- **Quiet commands:** running `/memory-cleanup` or `/deleate`, and your short answers to them ("1-5, T2", "yes", "show T3"), are not saved to memory. Add more with `HYPNOS_QUIET_COMMANDS="cmd1,cmd2"`.
- **Small coding memory:** before saving, each fact is checked: would a later session need this, and can't it find it in the files? (System One does the check, or the memory model in one call when System One is unavailable.) One-off steps, a tool's internals, run counts and session titles are dropped. Facts you state (`memory_remember`, pin) are always kept.
- **Cleanup tools:** `memory_cleanup_view(space)` and `memory_cleanup_apply(selection, space, delete_source)`. Claude passes the space name shown after "Memory for" in the list, so the numbers mean the same facts.
- **System One problems:** if the System One account refuses (no credits, bad key), Hypnos logs a warning and says so in the wake note; `memory_status` shows it as `systemone_problem`.
- **Sleep timing:** on compaction, at session end, when a new session starts, after 10 idle minutes, or once 150 new rows pile up.
- **Safety:** hooks never block or fail a session. If the service is down they exit silently.
- **No recursion:** when hypnos uses `claude -p` as its writer, it sets `HYPNOS_DISABLE=1`, so those runs don't trigger the hooks again, and passes `--no-session-persistence`, so they never appear in your Claude Code session list.
