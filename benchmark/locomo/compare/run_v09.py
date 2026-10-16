"""LoCoMo: v0.9 against v0.8 on the same 200 questions.

v0.9 changes how memories are built (each line's date is shown to the writer, relative dates become absolute) and labels
raw messages oldest-first, so its memories are rebuilt from scratch in a separate home (.agentmem-v09) with the same writer
(Muse Spark 1.3), Jev and vectors as v0.8. v0.8's answers are the saved ones from results_v08.json. Both answers to each
question are graded side by side, anonymised, by Claude Haiku 5.5.
  MAX_Q=5 CONVS=1 ...  smoke test on one conversation
  uv run --no-project --python 3.12 --with fastembed --with-editable ../../../hypnos python run_v09.py
"""
from __future__ import annotations
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run_locomo as R                                          # noqa: E402
from hypnos.config import Config                              # noqa: E402
from hypnos.llm import OpencodeWriter                         # noqa: E402

MAX_Q = int(os.environ.get('MAX_Q', '0'))
CONVS = [int(c) for c in os.environ.get('CONVS', '').split(',') if c.strip()]
OUT = HERE / 'results'
SYSTEMS = ['v0.8', 'v0.9']
TAG = os.environ.get('TAG', 'v09')                    # v09b: rerun with the date changes and recall labels removed


def main():
    data = json.loads((R.HERE / 'locomo10.json').read_text(encoding='utf-8'))
    prev = json.loads((OUT / 'results_v08.json').read_text(encoding='utf-8'))
    if CONVS:
        prev = [x for x in prev if x['conv'] in CONVS]
    if MAX_Q:
        prev = prev[:MAX_Q]
    cfg = Config(home=R.HERE / f'.agentmem-{TAG}', writer='opencode', jev=True, vectors=True)
    cfg.spaces_dir.mkdir(parents=True, exist_ok=True)
    writer = R.Cached(OpencodeWriter(cfg), 'muse')
    judge_w = R.Cached(R.HaikuJudge(), 'haiku55')
    mems = {i: R.build_memory(i, data[i], writer, cfg) for i in sorted({x['conv'] for x in prev})}   # one at a time

    def run(x):
        i = x['conv']
        q = next(q for q in data[i]['qa'] if q['question'] == x['question'])
        r = mems[i].recall(x['question'], budget=4000)
        ans = {'v0.8': x['answers']['agentmem_v08'], 'v0.9': R.answer(writer, data[i], x['question'], r.context)}
        g = R.judge(judge_w, q, ans)
        R.log(f'[{x["category"]:<11}] v0.8={"Y" if g["v0.8"] else "n"} v0.9={"Y" if g["v0.9"] else "n"}  level={r.level}')
        return {'conv': i, 'category': x['category'], 'question': x['question'], 'gold': x.get('gold', q['answer']),
                'answers': ans, 'correct': g, 'level': r.level,
                'ctx_tokens': {'v0.8': x['ctx_tokens']['agentmem_v08'], 'v0.9': r.tokens}}
    with ThreadPoolExecutor(R.WORKERS) as ex:
        res = list(ex.map(run, prev))

    rep = {'n': len(res), 'answerer': 'muse-spark-1.3', 'judge': 'claude-haiku-5-5 (both answers graded together, anonymised)',
           'accuracy': {}, 'by_category': {}, 'ctx_tokens': {}, 'not_mentioned': {},
           'memory_v09': {i: m.status() for i, m in mems.items()}, 'levels_v09': dict(Counter(r['level'] for r in res))}
    for s in SYSTEMS:
        rep['accuracy'][s] = round(100 * sum(r['correct'][s] for r in res) / len(res), 1)
        rep['ctx_tokens'][s] = round(sum(r['ctx_tokens'][s] for r in res) / len(res))
        rep['not_mentioned'][s] = sum(1 for r in res if r['answers'][s].strip().lower().startswith('not mentioned'))
        rep['by_category'][s] = {c: round(100 * sum(r['correct'][s] for r in res if r['category'] == c) /
                                          max(1, sum(1 for r in res if r['category'] == c)), 1) for c in R.CATS.values()}
    rep['calls'] = dict(R.STATS)
    tag = '_smoke' if (MAX_Q or CONVS) else ''
    (OUT / f'report_{TAG}{tag}.json').write_text(json.dumps(rep, indent=1, default=str), encoding='utf-8')
    (OUT / f'results_{TAG}{tag}.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    R.log('REPORT ' + json.dumps(rep, default=str))


if __name__ == '__main__':
    main()
