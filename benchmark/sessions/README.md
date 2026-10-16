# Benchmark harness

Every model call is cached in `cache/`, so rerunning any script replays saved results for free and only pays for new calls.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python)
- [opencode](https://opencode.ai) with the free model `opencode/muse-spark-1.3-contributor-free`. The free tier only accepts a **stock** opencode config: the scripts point `XDG_CONFIG_HOME` at an empty `cfg/` folder and call `opencode run`.
- A Jev key in `~/.typesafe/credentials` as `TYPESAFE_API_KEY=...`
- `bench.py` reads the source sessions from `~/.claude/projects/<your-project-folder>`; change `SESS_DIR` and `SESSIONS` for other machines. The session text, answers and call cache from the original run are private and not included, so a run here makes fresh calls.

## Run

```bash
WORKERS=2 uv run --no-project python bench.py        # A, B, D
WORKERS=2 uv run --no-project python bench_e.py      # E (v0.4)
WORKERS=2 uv run --no-project python bench_f.py      # F (v0.5)
WORKERS=2 uv run --no-project python bench_g.py      # G (plain opencode)
NO_GRADER2=1 WORKERS=2 uv run --no-project --python 3.12 --with fastembed python bench_h.py   # H (v0.6)
```

`CACHE_ONLY=1` builds a report from saved calls without making any new ones. `NO_GRADER2=1` skips the Muse Spark 1.2 cross-check grader.

## Notes

- The Muse Spark free quota appears to reset daily at 00:00 UTC; a full run of one system is about 180 calls.
- `bench_h.py` builds a local `bge-small` vector index on the CPU; it is saved in `cache/` (`emb_*.npy`) and embeds in small batches to stay light on RAM.
- Reports: `out/report.json` (A, B, D), `report_e.json`, `report_f.json`, `report_g.json`, `report_h.json` (all six systems graded together).
