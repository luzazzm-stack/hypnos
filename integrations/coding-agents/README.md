# Memory for any coding agent

Claude Code and opencode have their own guides ([claude-code](../claude-code/README.md), [opencode](../opencode/README.md)). Every other coding agent connects in one or more of three ways. Use as many as your agent supports.

| Way | What you get | Works with |
|---|---|---|
| **1. MCP server** | Tools the agent calls on purpose: recall, remember, note, review, cleanup, forget | every agent that speaks MCP |
| **2. Hooks** | Automatic memory: wake at start, recall on every prompt, every tool call logged, sleep at the end | agents with a hook system |
| **3. Model proxy** | Automatic memory for agents with neither: memory goes into the system prompt and every turn is logged | any agent that lets you set an OpenAI-compatible base URL |

First, install once:

```bash
uv tool install --python 3.12 "./hypnos[mcp,vectors]"    # puts `hypnos` on PATH
```

`hypnos setup` connects Codex, Gemini CLI, Cursor and Windsurf for you (MCP server, Gemini CLI's hooks, and the memory lines in `~/.codex/AGENTS.md` and `~/.gemini/GEMINI.md`). The tables below are for doing it by hand or for other agents.

The memory service starts by itself the first time a hook or the MCP server needs it, and stops about 90 seconds after the last agent closes (`stay_running = true` in `~/.hypnos/config.toml` keeps it on). Every agent on the machine shares it, so a Codex session can recall what a Cursor session learned in the same project. Memory is kept per git repository or folder (`code-<repo or folder name>`), and a session keeps the memory of the folder it started in.

## 1. MCP server (any MCP agent)

The command is always `hypnos mcp`. Where it goes:

| Agent | Config file | Entry |
|---|---|---|
| Codex CLI | `~/.codex/config.toml` | `[mcp_servers.hypnos]` with `command = "hypnos"`, `args = ["mcp"]` |
| Gemini CLI | `~/.gemini/settings.json` | `"mcpServers": {"hypnos": {"command": "hypnos", "args": ["mcp"]}}` |
| Cursor | `~/.cursor/mcp.json` | `"mcpServers": {"hypnos": {"command": "hypnos", "args": ["mcp"]}}` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | same `mcpServers` entry |
| Cline, Roo Code, Kilo Code | the extension's MCP settings (`cline_mcp_settings.json` or similar) | same `mcpServers` entry |
| GitHub Copilot (VS Code) | `.vscode/mcp.json` | `"servers": {"hypnos": {"type": "stdio", "command": "hypnos", "args": ["mcp"]}}` |
| Continue | `config.yaml` | `mcpServers: [{name: hypnos, command: hypnos, args: [mcp]}]` |
| Zed | `settings.json` | `"context_servers": {"hypnos": {"command": {"path": "hypnos", "args": ["mcp"]}}}` |
| Goose, Amp, Kiro, Warp, others | their MCP / extensions settings | command `hypnos`, argument `mcp` |

Config file names move between versions; if one doesn't match, look for "MCP" in your agent's settings. The entry is always the same two things: command `hypnos`, argument `mcp`.

Then add these lines to the agent's rules file (`AGENTS.md`, `GEMINI.md`, `.cursor/rules`, `.windsurfrules`, `.clinerules` or the like). Without hooks, this is what makes the agent use its memory:

```markdown
## Long-term memory
- At the start of a task, call memory_recall with what the task is about.
- When the user states a preference or decision worth keeping, call memory_remember (everywhere=true if it applies to all projects).
- At the end of a task, call memory_note with what was done, decided, learned, or left open.
- Exact values (paths, ports, commands) come from the quoted evidence; re-check anything marked STALE.
```

## 2. Hooks (agents with a hook system)

`hypnos hook generic <event>` works with any hook system. It reads the event JSON (or plain text) on stdin, or as the last argument. For `start` and `prompt`, it prints the memory to add as plain text. The field names of the common agents are understood: `session_id`, `prompt`, `tool_name`, `tool_input`, `tool_response`, `transcript_path`, `last-assistant-message` and so on.

| Event | Run it when | Other agents' names for it |
|---|---|---|
| `start` | a session starts | SessionStart |
| `prompt` | the user sends a message | UserPromptSubmit, BeforeAgent, beforeSubmitPrompt |
| `tool` | a tool call or file edit finished | PostToolUse, AfterTool, afterFileEdit, afterShellExecution |
| `reply` | the agent finished its turn | Stop, AfterAgent, agent-turn-complete |
| `compact` | the context is about to be compacted | PreCompact, PreCompress |
| `end` | the session ends | SessionEnd |

You can pass the agent's own event name; it is mapped for you.

Examples:

- **Gemini CLI**, in `~/.gemini/settings.json` `hooks`: `SessionStart` → `hypnos hook generic start`, `BeforeAgent` → `hypnos hook generic prompt`, `AfterTool` → `hypnos hook generic tool`, `AfterAgent` → `hypnos hook generic reply`, `PreCompress` → `hypnos hook generic compact`, `SessionEnd` → `hypnos hook generic end`.
- **Cursor**, in `~/.cursor/hooks.json`: `afterFileEdit` and `afterShellExecution` → `hypnos hook generic tool`, `stop` → `hypnos hook generic reply`. Cursor's `beforeSubmitPrompt` can't add context, so recall goes through MCP (way 1).
- **Codex CLI**, in `~/.codex/config.toml`: `notify = ["hypnos", "hook", "generic"]`. Codex passes `{"type": "agent-turn-complete", ...}` as the last argument, which logs the turn. Recall goes through MCP (way 1).
- **Your own scripts**: `echo "what port does staging use?" | hypnos hook generic prompt`

Hooks never block or fail the agent: if anything goes wrong they exit quietly.

## 3. Model proxy (agents with neither)

```bash
hypnos chat-proxy --upstream https://api.openai.com/v1 --space code-myproject    # listens on http://127.0.0.1:7440/v1
```

Point the agent's OpenAI-compatible base URL at `http://127.0.0.1:7440/v1`, for example Aider with `--openai-api-base http://127.0.0.1:7440/v1`. Every request gets the recalled memory in its system prompt, and every turn is logged. For a read-only snapshot instead: `hypnos export --space code-myproject > MEMORY.md`, then let the agent read that file (`aider --read MEMORY.md`).

## Many sessions at once

Running five Claude Code, opencode, Codex and Cursor sessions side by side is fine:
- One memory service serves all of them, and SQLite allows one writer at a time.
- Each session's own live messages are never recalled back to it. Other sessions' messages in the same project are, so agents see what the others decided.
- Each session knows the others: with every prompt it's told which other sessions are working in the project right now and what each was asked, with a reminder that their tasks are not its own. Remembered items from those sessions are marked "from another session running now: not your task", and memory is always presented as background, never as instructions.
- A session is consolidated (sleep) only after it has been quiet for 10 minutes or when it ends. Busy sessions are left alone.
- Sleeps run one at a time across the whole machine, in a queue, so the writer models are never hit in parallel.
- Jev requests are capped at 24 in flight for all sessions together (`HYPNOS_JEV_MAX_IN_FLIGHT`).
