# hypnos for assistants, automations, search, browsing and custom agents

All adapters live in `hypnos.integrations.agents`. Each works in two ways:
- **in-process:** `use_service=False`, the default
- **through the local service:** `use_service=True`, which is best when several processes share one memory

| Adapter | Profile | Logs | Evidence and freshness |
|---|---|---|---|
| `AgentMemory` | generic | user and assistant turns, tool calls | tool records |
| `AssistantMemory` | assistant | turns, tool calls, `commitment(what, due, to)` | promises go stale after 60 days |
| `AutomationMemory` | automation | `run(workflow, step, outcome, ok)`, `fix(workflow, problem, fix)`; `before_run(workflow)` recalls | `run:<workflow>@<time>`; endpoints stale after 90 days (see [automation](../automation/README.md)) |
| `SearchMemory` | search | queries, results with page text | `url:<url>@<date>`; claims go stale after 30 days |
| `BrowsingMemory` | browsing | steps, selectors, outcomes, page text (redacted) | `url:<url>`; selectors stale after 14 days, how-tos after 60 |
| `RobotMemory` | robotics | observations, actions | `obs:<sensor>@<time>` (see [robotics](../robotics/README.md)) |

## Custom agent loop

```python
from hypnos.integrations.agents import AgentMemory

mem = AgentMemory("my-agent", session="run-42")
system = mem.system_prompt("You are a helpful agent.", user_message)   # adds recalled memory
mem.user(user_message)
# ... call your model; for each tool call:
mem.tool("search_flights", {"from": "KTM", "to": "DEL"}, result)
mem.assistant(final_answer)
mem.sleep()                                                             # at task end
```

## Search agent

```python
from hypnos.integrations.agents import SearchMemory
mem = SearchMemory("research-gpu")
mem.query("Vega 8 shader count")
mem.result(url, title, page_text)          # the URL and date become the fact's evidence
print(mem.recall("How many shader units does the Vega 8 have, and which source said so?"))
```

## Browsing agent

```python
from hypnos.integrations.agents import BrowsingMemory
mem = BrowsingMemory("browser")
mem.step("https://example.com/login", "clicked 'Sign in with Google'", "redirected to consent page", selector="button#google")
print(mem.howto("example.com", "log in"))
```

Any agent that speaks **MCP** can also use the tools directly: `hypnos mcp` works with Claude Desktop, Cursor, Continue and others.
