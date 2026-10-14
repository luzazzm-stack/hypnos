# Which version for which system

One memory engine, with a version tuned for each kind of AI system. A version (a "profile") decides what is worth remembering, what counts as evidence, and when a fact goes out of date. Recall, sleep, Jev routing, cleanup and the safety rules are the same everywhere.

| System | Version (profile) | Remembers | Goes out of date | How to connect |
|---|---|---|---|---|
| **Coding agents**: Claude Code, opencode, Codex, Gemini CLI, Cursor, Windsurf, Cline, Copilot, Aider, ... | `coding` | paths, commands, settings, conventions, decisions, fixes, failed approaches | when git shows the cited file changed | [claude-code](claude-code/README.md), [opencode](opencode/README.md), [any other coding agent](coding-agents/README.md) |
| **AI agents and assistants**: email, calendar, support, personal helpers | `assistant` | preferences, people, promises and deadlines, decisions, plans | promises after 60 days | [agents](agents/README.md) (`AssistantMemory`) |
| **Automation agents**: n8n, Zapier, Make, RPA, scheduled jobs | `automation` | workflows, triggers, endpoints and quirks, rate limits, failures and the fixes that worked | endpoints after 90 days, limits after 30 | [automation](automation/README.md) (`AutomationMemory` or the HTTP API) |
| **Robots** | `robotics` | where objects were seen and when, maps, skills, failures, calibration, safety | object locations after ~1 hour, calibration 14 days, maps 30 days | [robotics](robotics/README.md) (`RobotMemory`) |
| **Research and search agents** | `search` | claims with their source URL and date, source quality, conflicts | claims after 30 days | [agents](agents/README.md) (`SearchMemory`) |
| **Browsing agents** | `browsing` | how to do things on each site, selectors, what broke | selectors after 14 days, how-tos 60 days | [agents](agents/README.md) (`BrowsingMemory`) |
| **Chat apps and any LLM app** | `chat` | the user's preferences, facts about them, projects, plans | no expiry | [chat](chat/README.md) (OpenAI-compatible proxy: change one URL) |
| **Anything else** | `generic` | values, names, decisions, outcomes, lessons | no expiry | Python (`AgentMemory`), the HTTP API on `127.0.0.1:7439`, MCP (`hypnos mcp`), or `hypnos hook generic` |

## Four ways in, for any system

1. **MCP server** (`hypnos mcp`): for any MCP client. Tools for recall, remember, note, review, cleanup, forget, purge, pause.
2. **Hooks** (`hypnos hook claude|generic <event>`): automatic memory for agents with a hook system.
3. **OpenAI-compatible proxy** (`hypnos chat-proxy`): for any app or agent that talks to an LLM API. Change the base URL and it has memory.
4. **Python and HTTP**: `from hypnos.integrations.agents import ...`, or JSON over HTTP to the local service (`/log`, `/recall`, `/sleep`, `/pin`, `/review`, `/cleanup`, `/forget_ids`, `/purge`, `/pause`) from any language.

## The same everywhere

- **Sleep writers fall back.** Free models first, then a small paid one (Claude Haiku), and a bigger one only if all the others are down. A failing model rests for 30 minutes (`writer_chain` in `~/.hypnos/config.toml`).
- **Many agents at once.** One local service serves every agent and session. Sleeps run in a queue, and a session's own live conversation is never recalled back to it.
- **You stay in charge.** Review and clean up with `/memory-cleanup` or the review and cleanup tools. Pause memory, forget a fact together with its original message, or purge for good.
