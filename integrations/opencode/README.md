# hypnos for opencode

Three parts, all optional and independent:
- a **plugin** for automatic memory
- the **MCP server** for memory tools the agent can call on purpose
- the **`/memory-cleanup` command**: lists every fact numbered under its topic (T1, T2...), newest first, plus numbered duplicates, unused-for-a-month and stale facts; delete by number, range, topic ("1-5, T2"), "all duplicates", "all unused", "all stale", or "purge". Running it, and your short answers to it ("1-5, T2", "yes"), are never saved to memory; nor is Hypnos's own output (`HYPNOS_QUIET_COMMANDS` adds more commands).

## Install

`hypnos setup` does all three for you (see [INSTALL.md](../../INSTALL.md)). By hand:

```bash
uv tool install --python 3.12 "./hypnos[mcp,vectors]"   # puts `hypnos` on PATH

# plugin (global)
cp integrations/opencode/hypnos.ts ~/.config/opencode/plugins/hypnos.ts

# /memory-cleanup command
cp integrations/opencode/command/memory-cleanup.md ~/.config/opencode/command/
```

Then add the MCP server to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "hypnos": { "type": "local", "command": ["hypnos", "mcp"], "enabled": true, "timeout": 10000 }
  }
}
```

## How it maps to the architecture

| Plugin hook | Architecture step |
|---|---|
| `chat.message` | log the user's message; **G1** gate, then **v0.8 recall** (System One-routed: the smallest context that answers) once per message, at most 800 tokens and only items this session has not been given yet |
| `experimental.chat.system.transform` | add the recalled memory to the system prompt of every model call in that turn |
| `tool.execute.after` | log the tool call and output, with `file:` evidence for file tools |
| `session.compacted` / `session.deleted` | **sleep** in the background |

The service also sleeps on its own when a new session starts, after 10 idle minutes, or once 150 new log rows pile up. opencode has no dedicated "session end" event, so that covers it.

## Notes

- **Free Zen models:** the plugin changes opencode's config, and the free tier only accepts a stock config. So don't enable the plugin in the config hypnos uses for its *own* writer calls; hypnos already keeps that in a separate `~/.hypnos/opencode-cfg`.
- **Assistant replies:** these aren't logged yet. Tool outputs and user messages carry most of the facts.
