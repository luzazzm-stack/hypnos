# Benchmarks

| Benchmark | What it measures | Result |
|---|---|---|
| [Own coding sessions](sessions) | 60 questions from 15 real Claude Code sessions; keyword search, rolling summary, plain opencode and Hypnos v0.3-v0.6 (Muse Spark 1.3 wrote, answered and graded) | v0.6 96% (in-sample). The session logs, answers and call cache stay private (they contain real work); the harness is public. |
| [LoCoMo](locomo) | 200 questions (50 each: single-hop, multi-hop, temporal, open-domain) over 10 long conversations | Hypnos v0.6 vs keyword search |
| [LoCoMo head-to-head](locomo/compare) | Same 200 questions: Hypnos v0.6-v0.9 (Python and Rust engines) vs Mem0 and GBrain | answered by Claude Opus 5.5: Hypnos v0.8 73%, Mem0 70%, GBrain 67%. Answered by Muse Spark 1.3: v0.8 55%, Mem0 51%, GBrain 51%. v0.9 ties v0.8 (60.5% / 60.5% in the same run). |

Every LoCoMo run grades all systems' answers to a question together, side by side and anonymised, with a grader from another model family (Claude Haiku). Per-question answers and grades are in `locomo/results/` and `locomo/compare/results/`. Write-up: [docs/RESULTS.md](../docs/RESULTS.md) and the [blog post](../docs/blog.html).

## Running it yourself

1. Download the LoCoMo data (`locomo10.json`) from https://github.com/snap-research/locomo into `benchmark/locomo/`.
2. See the header of each `run_*.py` for the exact command. The harness caches every model call, so a stopped run resumes where it left off.
3. For the session benchmark, point `SESS_DIR` and `SESSIONS` in `sessions/bench.py` at your own Claude Code sessions; see [sessions/README.md](sessions/README.md).
