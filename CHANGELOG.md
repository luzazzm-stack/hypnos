# Changelog

## v0.9.6: smaller chats: 2026-10-19

Found on a machine where one never-restarted chat used about 1.2 billion tokens in two days. Memory added to every message stays in the chat, so in a long chat it is re-read again and again.

- Memory added to each message is smaller: 800 tokens at most (was 2,500). Change it with `prompt_tokens`.
- A chat never gets the same memory twice. Hypnos remembers what it already gave each session and sends only new items. After a compaction the agent has forgotten them, so they can come again.
- Long-chat notice: once the agent's context passes 150,000 tokens (`long_chat_tokens`, 0 turns it off), the agent is asked to tell the user that `/clear` or `/compact` would make each reply much cheaper. It says so once per 50,000 tokens.
- The service follows edits to `~/.hypnos/config.toml`. Before, a service started before setup finished kept the old settings until it restarted, and kept trying writers the user had removed.
- Every session now starts with a short "how your memory works" note, so the agent knows Hypnos saves on its own (no handover files needed), how to look things up and pin things, that sleep never shrinks the chat, and that exact values come from the cited source.
- Setup writes the full path to `hypnos` into agent hooks and MCP settings. A bare `hypnos` failed silently when an agent was started without the shell PATH, and memory then did nothing.
- Settings files with a BOM (as PowerShell and some Windows editors write them) load fine; before, Hypnos could not read its settings at all.
- Default writer list updated: Muse Spark and Ling are gone from opencode's free models, so the free choices are now MiMo, Nemotron Lightning and DeepSeek Flash, then Claude Haiku and Sonnet.
- `systemone_model` with `systemone = "laya"` names the Laya checkpoint to use (for example one fine-tuned for Hypnos).

## v0.9.5: fixes from real use (no duplicates, small memory, usable cleanup): 2026-10-16

- A fact is never stored twice. A new fact that only restates a stored one is dropped. System One gets a "repeat" answer in the check it already makes for every new fact; without System One, plain code compares meaning (local embeddings), words, and every number, date, version, name, file, id and ordinal. Any difference in those keeps both facts ("Level 6" vs "Level 7", "v0.8" vs "v0.9", "10 emails" vs "20 emails", "VS Code" vs "Neovim").
- `/memory-cleanup`, `/deleate` and short answers to them ("1-5, T2", "yes") are no longer logged, in Claude Code and opencode. Hypnos also never stores its own output (cleanup lists, wake notes, recalled memory). Add more commands with `HYPNOS_QUIET_COMMANDS`.
- `/memory-cleanup` lists every fact, numbered under its topic, where it used to show only topic counts. Lines are cut at a word, and "show Tn" numbers the older facts too. Facts that differ in a number, name or id are no longer offered as duplicates.
- A session keeps the memory of the folder it started in, even after it changes folder, so logging, recall, the cleanup list and deleting all use the same memory. Before this, "delete 3" could hit a different list.
- Memory-writer calls through `claude -p` no longer show up in the Claude Code session list (`--no-session-persistence`).
- Coding memory stays small. It keeps only what a later session needs and cannot read in files (rules, decisions, where things are, outcomes, things written nowhere), and drops one-off steps, a tool's internals, run counts and session titles before saving. Facts the user states are always kept.
- Without System One, the memory model picks a fact's topic. The old 3-shared-words guess filed facts under unrelated topics.
- When the System One account refuses (no credits, bad key), Hypnos logs it and says so in the wake note. It used to fall back to weaker checks silently.
- One-time clean-up of existing memory on this laptop: Claude Haiku sorted 3,217 facts into keep and junk in two passes; 2,741 junk facts were archived (restorable) and 56 logged housekeeping runs wiped.

## v0.9: usable by anyone (one-line install, any agent, any model): 2026-10-13 to 2026-10-15

- Renamed to Hypnos (was `agentmem`): command `hypnos`, data in `~/.hypnos` (moved over from `~/.agentmem` automatically), settings `HYPNOS_*` (`AGENTMEM_*` still read), MCP server `hypnos`.
- One-line install: `install.sh` (macOS/Linux) and `install.ps1` (Windows) bring uv and Python 3.12, install Hypnos and run setup.
- `hypnos setup` connects Claude Code, opencode, Codex, Gemini CLI, Cursor and Windsurf and removes old agentmem entries. `--uninstall` undoes everything; `--yes` runs it without questions, for agents and scripts.
- No model is fixed. `--list-models` lists every model the machine can use (opencode, Ollama, Claude, Codex, API keys) and suggests the best value; `--memory-model` sets the fallback chain.
- System One can be Jev (hosted), Laya (local, free) or none, in which case the memory model makes the checks: a little more accurate, more tokens.
- Memory runs only while an agent is open and stops about 90 s after the last one closes; `stay_running = true` keeps it on for servers and robots. Every background process is hidden (no windows).
- The `/memory-cleanup` list is prepared in the background and opens in under a second (`hypnos cleanup`); deletes run in one transaction.
- Sleep writes facts from several chunks at once and checks claims in batches. Machines with less than 12 GB of RAM use one writer at a time.
- "The assistant can't do X" is never saved.
- opencode writer runs no longer appear in the user's opencode history.
- Reverted after LoCoMo: a first v0.9 gave the fact writer the date on every log line, asked it to turn relative dates into absolute ones, and told the answering model that later messages override earlier ones. It scored 47.0% against v0.8's 57.5%, so all three were removed. The fixed v0.9 tied v0.8 at 60.5% (Muse Spark 1.3 answering, Claude Haiku 5.5 grading).
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
