# Results

Two evaluations, reported separately because they measure different things.

## 1. Own sessions (in-sample)

**What was measured:**
- **Data:** 60 questions written from 15 real Claude Code sessions. Every answer is proven by an exact quote.
- **Models:** Muse Spark 1.3 (through stock opencode) wrote the questions, built memory, answered, and graded. Muse Spark 1.2 cross-checked the grading (99% agreement), except for v0.6.
- **Tuning:** v0.4–v0.6 were designed after seeing earlier failures on these same questions. **These numbers are in-sample.**

| System | Accuracy | Confident wrong | Tokens read / answer |
|---|---|---|---|
| hypnos v0.6 | **96%** | 0 | 3,531 |
| Keyword search over raw logs | 90% | 1 | 2,258 |
| hypnos v0.5 | 90% | 1 | 4,070 |
| hypnos v0.4 | 87% | 2 | 3,457 |
| hypnos v0.3 | 82% | 1 | 4,276 |
| Rolling summary | 36% | 0 | 3,185 |

**Plain opencode** (no memory system; it greps and reads the logs itself) was measured on 24 questions from the 6 oldest sessions only, because of free-tier quota:

| System | Accuracy | Tokens read / question | Steps / question |
|---|---|---|---|
| Plain opencode | 92% | ~35,000 | ~7 |
| hypnos v0.6 | 100% | ~3,700 | 1 |

**Cost to build memory:**
- hypnos v0.4–v0.6: about 0.78M Muse tokens for all 15 sessions. About 0.66M of that is with Jev handling verify and topic choice.
- Keyword search: none.

**Jev against Muse Spark, on the same decisions:**

| Decision | Jev speed | Agreement |
|---|---|---|
| Verify | 15.5× faster | 86% |
| Recall enough? | 11.3× faster | 76% |
| Topic choice | 6.9× faster | 90% |

The harness is in [`benchmark/sessions`](../benchmark/sessions). The session logs, per-question answers and call cache are private (they contain real work), so only these results are published.

## 2. LoCoMo (standard benchmark)

**What is being measured:**
- **Data:** [LoCoMo](https://github.com/snap-research/locomo) (Maharana et al., 2024), 10 long two-person conversations. 200 questions, 50 per category: multi-hop, temporal, open-domain, single-hop. The adversarial category is excluded, as in most published results.
- **Models:** the same answering model for every system (Muse Spark 1.3). **Graded by Claude Haiku**, a different model family, with LoCoMo-J style CORRECT/WRONG.
- **Systems:**
  - hypnos, built from the conversations with sleep
  - keyword search over the raw turns, top 10
  - (a full-context upper bound was planned but not run, to stay within the free Muse quota)

### v0.9 vs v0.8 (2026-10-14)

Same 200 questions, answered by Muse Spark 1.3, both answers per question graded blind and side by side by **Claude Haiku 5.5**. v0.9's memories were rebuilt from scratch; v0.8's answers are the saved ones from its own run, re-graded in each run.

| | v0.8 | v0.9, first try | v0.9, fixed |
|---|---|---|---|
| Overall | 57.5% / 60.5% | **47.0%** | **60.5%** |
| Single-hop | 84% / 84% | 74% | 82% |
| Multi-hop | 46% / 52% | 32% | 48% |
| Temporal | 80% / 82% | 60% | 80% |
| Open-domain | 20% / 24% | 22% | 32% |
| "Not mentioned" answers | 46 | 61 | 43 |
| Context per question | ~602 tokens | ~569 tokens | ~611 tokens |
| Facts saved, conversation `locomo-1` | 160 | 93 | 151 |

v0.8's two numbers are from the first and the second run (the grader re-marked 8 of its unchanged answers). The first v0.9 added three things: the date on every log line given to the fact writer, an instruction to turn relative dates into absolute ones, and a "later messages override earlier ones" note for the answering model. The small writer then saved far fewer facts (v0.9 lost 26 questions to v0.8 and won 5; 14 of the losses were "not mentioned"). With all three removed, v0.9 tied v0.8 exactly (121 correct each; each won 8 the other lost). Rule since then: no change to the writer or answering instructions ships without beating the previous version. Harness: `benchmark/locomo/compare/run_v09.py` (`TAG=v09b` for the rerun).

### v0.6 vs keyword search (2026-09-28)

Answered by Muse Spark 1.3, graded by Claude Haiku.

| Category (50 Q each) | hypnos | keyword search |
|---|---|---|
| **Overall (200 Q)** | **41.5%** | 33.5% |
| Multi-hop | **28%** | 8% |
| Temporal | **68%** | 64% |
| Single-hop | **58%** | 50% |
| Open-domain | 12% | 12% |
| Avg. context per question | ~2,240 tokens | ~380 tokens |

**What this shows:**
- hypnos is ahead by 8 points overall. The biggest gap is on multi-hop questions (3.5×), where facts linked by entities and topic summaries help.
- Retrieval is still the main weakness. hypnos replied "Not mentioned" 64 times (keyword: 88), so the answer was often not in the recalled context. Most recalls (129 of 200) fell through to whole-log search.
- In these two-person chats, topic assignment puts almost everything into 1–4 broad topics. That makes the small/medium/big ladder less useful here than on coding sessions.

Per-question results are in [`benchmark/locomo/results`](../benchmark/locomo/results).

### Answered by Claude Opus 5.5 (2026-09-29)

Same 200 LoCoMo questions, answered by **Claude Opus 5.5** (medium effort) instead of Muse Spark 1.3, so the scores sit on the same scale as published results. Contexts rebuilt from the same memories; **Claude Haiku 4.5** graded all four answers per question blind and side by side.

| System | Overall | Multi-hop | Temporal | Single-hop | Open-domain | Context / question | "Not mentioned" |
|---|---|---|---|---|---|---|---|
| **hypnos v0.8** | **73%** | **56%** | **92%** | 84% | 60% | **~610 tokens** | **12** |
| Mem0 2.2 OSS | 70% | 48% | 88% | 82% | **62%** | ~1,130 tokens | 16 |
| GBrain 0.59 | 67% | 38% | 88% | **90%** | 52% | ~3,980 tokens | 21 |
| Keyword search | 46.5% | 12% | 70% | 62% | 42% | ~380 tokens | 59 |

Run cost: 796 Opus 5.5 calls, 2.31M tokens ($19.11 at list price). Published LoCoMo numbers (Mem0 ~67-68%, Zep ~66-75%) use other answering models, graders and question subsets; compare loosely. Harness: `benchmark/locomo/compare/run_opus.py`.

### Rust engine vs Python engine (2026-09-29)

Python remains the main engine; the Rust version is frozen in `future/agentmem-rs` for later.

Same 200 questions, same memories, both engines running v0.8 recall while warm; answered by Muse Spark 1.3, all answers graded together by Claude Haiku.

| | With Rust (agentmem-rs) | Without Rust (Python) |
|---|---|---|
| Accuracy | 55% | 55% |
| Context per question | ~530 tokens | ~600 tokens |
| "Not mentioned" answers | 38 | 46 |
| Search time: median / average / slowest 10% | 3.0 s / 3.2 s / 3.4 s | 3.8 s / 4.0 s / 4.6 s |
| Search time, cold start | ~3.9 s | ~7.8 s |
| Peak RAM | ~290 MB (service) | ~250–300 MB (process) |

Harness: `benchmark/locomo/compare/run_rust.py` (split over two machines, merged by `merge_rust.py`).

### v0.8: Jev-routed recall (2026-09-29)

Same 200 questions, same memories, answered by Muse Spark 1.3; all six answers per question graded blind and side by side by Claude Haiku.

| System | Overall | Multi-hop | Temporal | Single-hop | Open-domain | Context / question |
|---|---|---|---|---|---|---|
| **hypnos v0.8** | **55%** | 44% | **78%** | 76% | **22%** | **~600 tokens** |
| hypnos v0.7 | 53% | 40% | 74% | **80%** | 18% | ~3,100 tokens |
| Mem0 2.2 OSS | 51% | **46%** | 66% | 72% | 20% | ~1,100 tokens |
| GBrain 0.59 | 51% | 32% | **78%** | 74% | 20% | ~4,000 tokens |
| hypnos v0.6 | 41% | 28% | 68% | 56% | 12% | ~2,200 tokens |
| Keyword search | 33.5% | 8% | 64% | 50% | 12% | ~400 tokens |

v0.8: code gathers candidates from every layer, Jev routes the question (overview / single fact / list / when / exact value), picks the smallest summary level that answers overviews, and gates every candidate fact and message. Harness: `benchmark/locomo/compare/run_v08.py` (run split over two machines with `SHARD`, merged by `merge_v08.py`).

### Head-to-head with Mem0 and GBrain (2026-09-28/29)

Same 200 questions, same answering model (Muse Spark 1.3), all five answers per question graded blind and side by side by Claude Haiku.

| System | Overall | Multi-hop | Temporal | Single-hop | Open-domain | Context / question |
|---|---|---|---|---|---|---|
| **hypnos v0.7** | **53.0%** | 38% | 74% | **82%** | 18% | ~3,100 tokens |
| GBrain 0.59 (hybrid, no reranker) | 51.0% | 30% | **78%** | 78% | 18% | ~4,000 tokens |
| Mem0 2.2 OSS | 50.0% | **42%** | 66% | 72% | **20%** | ~1,100 tokens |
| hypnos v0.6 | 40.5% | 26% | 68% | 54% | 14% | ~2,200 tokens |
| Keyword search | 33.5% | 8% | 64% | 48% | 14% | ~400 tokens |

v0.7 changed recall only (same stored memories as v0.6): evidence-first hybrid search over facts and raw messages, entity gathering, and a stricter gate. "Not mentioned" answers fell from 64 to 42 (Mem0 44, GBrain 45). With 200 questions the grading noise is about ±3 points, so v0.7 is best read as tied for first, slightly ahead. Harness and per-question results: [`benchmark/locomo/compare`](../benchmark/locomo/compare).

**Comparing with other systems:** published numbers for Mem0, Letta, Zep and others use different answering models, graders and question subsets. Only compare them loosely with these results.
