# Changelog

## v0.9.1: bug fixes from real use: 2026-10-16

- **Never a duplicate.** A new fact that only restates a stored one is not saved. System One gets a "repeat" answer in the check it already makes for every new fact; without System One, a plain-code check compares meaning (local embeddings), words, and every number, date, version, name, file, id and ordinal. Any difference in those keeps both facts ("Level 6" vs "Level 7", "v0.8" vs "v0.9", "10 emails" vs "20 emails", "VS Code" vs "Neovim").
- **Housekeeping stays out of memory.** `/memory-cleanup`, `/deleate` and short answers to them ("1-5, T2", "yes") are not logged, and Hypnos never stores its own output (cleanup lists, wake notes, recalled memory) again. More commands: `HYPNOS_QUIET_COMMANDS`.
- **`/memory-cleanup` lists every fact**, numbered under its topic, instead of only topic counts; lines are cut at a word; "show Tn" numbers the older facts too. Facts that differ in a number, name or id are no longer offered as duplicates.
- **One memory per session.** A session keeps the memory of the folder it started in, even after it changes folder, so logging, recall, the cleanup list and deleting all use the same memory (before, "delete 3" could hit a different list).
- **No ghost sessions.** Memory-writer calls through `claude -p` no longer appear in the Claude Code session list (`--no-session-persistence`).
- **No silent System One failure.** When the System One account refuses (no credits, bad key), Hypnos logs it and says so in the wake note instead of quietly falling back to weaker checks.

## v0.9 (in progress): memory you can manage, no echo, sooner sleep: 2026-10-13

- No echo: recall skips messages of the current conversation that the agent still sees (until a compaction), and skips Jev entirely when memory holds nothing else.
- Sooner sleep: when a new session starts, after 10 idle minutes (was 30), or at 150 new rows (was 400). PreCompact marks the compaction.
- Memory management: `memory_review`, `memory_cleanup_suggestions` (near-duplicates, unused for 30 days, stale, purgeable), `memory_forget_ids`, `memory_purge`, `memory_pause`; `/memory-cleanup` slash command for Claude Code and opencode.
- Remember everywhere: `memory_remember(fact, everywhere=True)` keeps a fact in a `global` space shown at every project's wake.
- Writer fallback chain (`writer = "auto"`, `writer_chain`): free opencode models first (Muse Spark, Nemotron Lightning, MiMo Flash, Ling Flash), then Claude Haiku, then Claude Sonnet only if all else is down; a failing writer rests 30 minutes. The opencode writer runs with no stdin again (the revert had removed that fix).
- Many sessions at once: sleeps run in one machine-wide queue without duplicates; only sessions quiet for 10 minutes (or ended) are consolidated, so busy parallel sessions are left alone; Jev requests capped at 24 in flight.
- Session awareness: each prompt is told which other sessions work in the project right now and what each was asked ("their tasks are not yours"); recalled items from those sessions are marked "not your task"; memory and the wake note are framed as background, never as instructions or a to-do list.
- Any coding agent: `hypnos hook generic <event>` understands the event names and fields of Claude Code, Gemini CLI, Cursor, Codex and others; `memory_note` MCP tool for agents without hooks; guide in `integrations/coding-agents`.
- Versions for every kind of system: new `assistant` and `automation` profiles with `AssistantMemory` and `AutomationMemory`; overview in `integrations/README.md`; automation guide with HTTP steps for n8n, Zapier and Make.

## Rust engine: 2026-09-29

- `future/agentmem-rs/`: the whole engine (v0.8) in Rust: storage, secret removal, hybrid search, local meaning search (bge-small, ONNX), Jev-routed recall, sleep consolidation, pin/correct/forget, and the HTTP service.
- Same database format, same HTTP API and the same prompts as the Python engine; existing memories and all integrations work unchanged.
- Decision (same day): Python stays the main engine. The Rust engine is frozen in `future/agentmem-rs` for later; the client uses it only with `HYPNOS_ENGINE=rust`.
- Parity on 20 LoCoMo questions: the answer is in the recalled context 17/20 for both engines; cold search ~3.9 s vs ~7.8 s.

## 0.8.0: 2026-09-29 (Jev-routed recall; LoCoMo 55% with ~600 tokens)

- Recall redesigned: code gathers candidates from every layer (`_gather`), Jev answers typed questions about them in parallel.
- Question routing (overview, single fact, list, when, exact value); overview questions get the smallest summary level Jev judges sufficient (small → medium → big).
- Per-item evidence gating: every candidate fact and message is judged by Jev; only answer-bearing items are sent, lists keep every relevant item.
- Answerable check expands with on-topic items when needed. `Jev.many()` sends several questions in one request.
- Average LoCoMo context: ~600 tokens (v0.7: ~3,100). v0.7 kept as `recall_v07()`.

## 0.7.0: 2026-09-28 (recall only; LoCoMo 53%, up from 40.5%)

Motivated by the LoCoMo head-to-head (Mem0 52%, GBrain 51%, hypnos v0.6 41.5%): recall that stopped at a big topic summary was right only 5 of 34 times.
- Evidence first: hybrid search (BM25 + vectors, fused with RRF) over facts and raw messages is the first recall step; summaries only orient.
- Meaning-search index over raw messages (`Memory.index_log()`, also run at sleep).
- Entity gather: every fact about the people and things named in the question, ranked by relevance (list and multi-hop questions).
- Stricter gate: stop early only when the evidence itself answers the question (Jev `ANSWERABLE`).
- Optional query expansion (`expand_query`, off by default).
- v0.6 recall kept as `Memory.recall_v06()`.

## 0.6.0: 2026-09-26

**Engine (`hypnos`):**
- Redacted, append-only log.
- Sleep pipeline: extract, verify against the quote, entities, contradiction resolution with validity windows, topic ladder, keyword anchors, wake note.
- v0.6 recall: keyword and vector search on every layer, exact-value rule (R15), evidence-only gate, one-hop entity expansion, lean deep steps.
- Real-time fact path, with de-duplication against sleep.

**Service and integrations:**
- Local service, MCP server (supports MCP 1.x and 2.x) and CLI.
- Claude Code hooks, opencode plugin, OpenAI-compatible chat proxy.
- Adapters for robotics (with a ROS 2 node), search, browsing and custom agents.

**Architecture and benchmarks:**
- Architecture spec v0.6: exact-value rule, evidence-only gate, original-log layer.
- Benchmark on 15 real sessions (v0.3–v0.6), and a LoCoMo benchmark harness.

## Earlier design versions

| Version | Date | Change | Result on own sessions |
|---|---|---|---|
| 0.5 | 2026-09-25 | per-topic original-log layer | 90–92% |
| 0.4 | 2026-09-25 | keyword search on every layer, anchors, topic splitting | 87–88% |
| 0.3 | 2026-09-25 | Jev gate and judge, trust order, entities, code-aware evidence (spec) | 82–83% |
| 0.2 | 2026-09-25 | fixed 12 design mistakes; small/medium/big topic ladder | n/a |
| 0.1 | 2026-09-21 | first spec: evidence-linked memory with sleep | n/a |
