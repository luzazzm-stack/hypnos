# Hypnos

**Human-like memory for AI agents.** Like a person, Hypnos notes everything during the day, sorts it into checked facts while it sleeps, and the next morning remembers what matters, with the evidence for every fact.

Works with Claude Code, opencode, Codex, Gemini CLI, Cursor, Windsurf and any agent with hooks or MCP, plus robots and automation through a small Python API.

## Install (one line)

**macOS / Linux**
```sh
curl -fsSL https://agent-memory-architecture-chi.vercel.app/install.sh | sh
```

**Windows** (PowerShell)
```powershell
irm https://agent-memory-architecture-chi.vercel.app/install.ps1 | iex
```

The installer brings everything it needs (uv, and Python through uv), connects the coding agents it finds, asks which System One model to use, and starts the memory in the background with no windows. Restart your agent afterwards. Asking a coding agent to install it? Point it at [INSTALL.md](INSTALL.md); the steps for agents are at the bottom.

## How it works

| Like a person | Hypnos |
|---|---|
| Notices everything during the day | every message and tool result is written to a raw log, instantly |
| Sleeps on it | when a session ends or goes quiet, a small model turns the log into short facts, each with the exact quote it came from; facts without evidence are dropped |
| Updates what it knows | a new fact that replaces an old one (a changed port, a new decision) retires the old one, with history kept |
| Remembers what matters | each new message gets only the memories it needs: a summary for broad questions, exact facts with quotes for precise ones |
| Forgets on request | `/memory-cleanup` shows duplicates, unused and stale facts by topic in under a second; delete by number or topic |

It never saves an AI model's refusals ("the assistant can't do X"), never blocks your agent, and keeps each project's memory separate. Several sessions can run side by side; each knows what the others are doing and that their tasks are not its own.

## Benchmarks

**LoCoMo** (200 questions over 10 long conversations), answered by Claude Opus 5.5, every system's answers graded together and blind by Claude Haiku 4.5:

| System | Overall | Multi-hop | Temporal | Single-hop | Open-domain | Memory per question |
|---|---|---|---|---|---|---|
| **Hypnos v0.8** | **73%** | **56%** | **92%** | 84% | 60% | **~610 tokens** |
| Mem0 2.2 OSS | 70% | 48% | 88% | 82% | **62%** | ~1,130 tokens |
| GBrain 0.59 | 67% | 38% | 88% | **90%** | 52% | ~3,980 tokens |
| Keyword search | 46.5% | 12% | 70% | 62% | 42% | ~380 tokens |

**Version by version**, same 200 questions answered by a small free model (Muse Spark 1.3), graded by Claude Haiku. Compare rows within a run:

| Version | LoCoMo | Memory per question | What changed |
|---|---|---|---|
| v0.1, v0.2 | not measured | | designs on paper |
| v0.3 to v0.6 | v0.6: 41% | ~2,200 tokens | on 60 questions from real coding sessions: v0.3 82%, v0.4 87%, v0.5 90%, v0.6 96% (in-sample) |
| v0.7 | 53% | ~3,100 tokens | evidence-first recall; ahead of Mem0 (51%) and GBrain (51%) in the same run |
| v0.8 | 55% | ~600 tokens | System One routes each question to the smallest context that answers |
| v0.9 | 60.5% (v0.8: 60.5% in the same run) | ~611 tokens | one-line install, any agent, cleanup, runs only while an agent is open. A first try with extra date instructions scored 47% and was reverted. |

Everything, including per-question results and how each run was measured: [docs/RESULTS.md](docs/RESULTS.md), the [benchmark harness](benchmark/), and the [blog post](https://agent-memory-architecture-chi.vercel.app/blog.html). The full architecture spec is in [docs/architecture.html](docs/architecture.html). A Rust version of the v0.8 engine (same accuracy, search 3.0 s vs 3.8 s) is kept in [future/agentmem-rs](future/agentmem-rs).

## System One model

Hypnos works best with a **System One model**, which answers yes/no and pick-one questions in one fast step instead of writing text. Memory uses it to decide what to keep and what to recall for a tiny fraction of an LLM's cost.

- **Jev** (TypeSafe): hosted, needs a key.
- **Laya**: open weights, free, runs on your machine. Running Laya locally next to your agent makes memory cost nothing extra.
- **None**: the memory model you pick also does these checks while saving. Answers can be a little more accurate, but it uses more tokens.

## Memory model

Writing facts needs an LLM. Hypnos isn't tied to one: setup lists every model your machine can use (free opencode models, a local Ollama, Claude, Codex, and any OpenAI, DeepSeek, Gemini, OpenRouter, Mistral, Groq or xAI key) and suggests the best value, free and small models first. If one is down or out of quota, the next takes over. On machines with less than 12 GB of RAM it runs one at a time so your computer stays fast.

## Commands

```
hypnos setup            connect agents, pick models (--list-models, --memory-model, --uninstall)
hypnos cleanup          the /memory-cleanup list
hypnos mcp              MCP server (memory_recall, memory_remember, memory_note, cleanup tools, ...)
hypnos hook ...         hook handler for Claude Code and other agents
hypnos serve            the background service (starts by itself)
```

More: [INSTALL.md](INSTALL.md), [integrations](integrations/README.md), [Python package](hypnos/README.md), [examples](examples/).

## Licence

Copyright (c) 2026 Luza Maharjan. All rights reserved. See [LICENSE](LICENSE).
