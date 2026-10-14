# Memory for automation agents

For n8n, Zapier, Make, RPA bots, scheduled jobs and other workflow agents. The `automation` version remembers:
- which workflows exist and what they do
- triggers and schedules
- API endpoints and their quirks, and rate limits
- failures, their causes, and the fixes that worked

So the next run avoids the same mistake. Never send secrets or tokens. Common patterns are masked before saving, but not everything.

## Python

```python
from hypnos.integrations.agents import AutomationMemory

mem = AutomationMemory("ops-automations", session="nightly-2026-10-13")
print(mem.before_run("invoice-sync"))           # recent failures, fixes, limits and quirks for this workflow
mem.run("invoice-sync", "POST /v2/invoices", "429 Too Many Requests", ok=False, endpoint="https://api.example.com/v2/invoices")
mem.fix("invoice-sync", "429 rate limit on bulk upload", "send at most 50 invoices per minute")
mem.run("invoice-sync", "POST /v2/invoices", "1,240 invoices synced", ok=True)
mem.sleep()                                     # at the end of the run
```

## No-code tools (n8n, Zapier, Make): HTTP

Start the service once with `hypnos serve` (it listens on `127.0.0.1:7439`). Then add HTTP request steps:

| When | POST to | Body (JSON) |
|---|---|---|
| Before the workflow | `/recall` | `{"space": "ops-automations", "profile": "automation", "query": "What should I know before running invoice-sync?"}` → use `context` |
| After a step | `/log` | `{"space": "ops-automations", "profile": "automation", "session": "<run id>", "role": "tool", "content": "[invoice-sync] POST /v2/invoices -> 429 FAILED"}` |
| A fix that worked | `/log` | same, with `"role": "agent"` and `"content": "[invoice-sync] problem: ...; fix that worked: ..."` |
| End of the run | `/sleep` | `{"space": "ops-automations", "profile": "automation", "session": "<run id>"}` |

The service listens on this computer only. If the automation tool runs elsewhere, run `hypnos serve` on that machine instead.
