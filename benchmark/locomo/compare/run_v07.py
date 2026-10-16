"""LoCoMo: agentmem v0.7 recall against the saved head-to-head (v0.6, Mem0, GBrain, keyword), same 200 questions.

Uses the memories already built for the main run (same facts, topics and entities: v0.7 changes recall only),
backfills the meaning-search index over raw messages, answers with the same model and prompt, and has Claude Haiku
grade all five answers to each question together.
  uv run --no-project --python 3.12 --with fastembed --with-editable ../../../hypnos python run_v07.py
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
from hypnos import Memory                                     # noqa: E402
from hypnos.config import Config                              # noqa: E402
from hypnos.llm import OpencodeWriter                         # noqa: E402

MAX_Q = int(os.environ.get('MAX_Q', '0'))
OUT = HERE / 'results'


def main():
    data = json.loads((R.HERE / 'locomo10.json').read_text(encoding='utf-8'))
    prev = json.loads((OUT / 'results.json').read_text(encoding='utf-8'))
    if MAX_Q:
        prev = prev[:MAX_Q]
    cfg = Config(home=R.HERE / '.agentmem', writer='opencode', jev=True, vectors=True)
    writer = R.Cached(OpencodeWriter(cfg), 'muse')
    judge_w = R.Cached(R.HaikuJudge(), 'haiku')
    mems = {}
    for i in sorted({x['conv'] for x in prev}):
        m = Memory(f'locomo-{i}', 'chat', cfg=cfg, writer=writer)
        m.index_log()
        mems[i] = m
        R.log(f'conv {i}: log index ready')

    def run(x):
        i = x['conv']
        q = next(q for q in data[i]['qa'] if q['question'] == x['question'])
        r = mems[i].recall(x['question'], budget=4000)
        ans = dict(x['answers'])
        ans['agentmem_v07'] = R.answer(writer, data[i], x['question'], r.context)
        g = R.judge(judge_w, q, ans)
        R.log(f'[{x["category"]:<11}] ' + ' '.join(f'{k}={"Y" if v else "n"}' for k, v in g.items()) + f'  level={r.level}')
        return {**x, 'answers': ans, 'correct': g, 'ctx_tokens': {**x['ctx_tokens'], 'agentmem_v07': r.tokens},
                'v07_level': r.level}
    with ThreadPoolExecutor(R.WORKERS) as ex:
        res = list(ex.map(run, prev))

    systems = ['agentmem_v07', 'mem0', 'gbrain', 'agentmem', 'keyword']
    rep = {'n': len(res), 'judge': 'claude haiku (all 5 answers graded together)', 'answerer': 'muse-spark-1.3',
           'accuracy': {}, 'by_category': {}, 'ctx_tokens': {}, 'not_mentioned': {},
           'v07_levels': dict(Counter(r['v07_level'] for r in res))}
    for s in systems:
        rep['accuracy'][s] = round(100 * sum(r['correct'][s] for r in res) / len(res), 1)
        rep['ctx_tokens'][s] = round(sum(r['ctx_tokens'][s] for r in res) / len(res))
        rep['not_mentioned'][s] = sum(1 for r in res if r['answers'][s].strip().lower().startswith('not mentioned'))
        rep['by_category'][s] = {c: round(100 * sum(r['correct'][s] for r in res if r['category'] == c) /
                                          max(1, sum(1 for r in res if r['category'] == c)), 1) for c in R.CATS.values()}
    rep['calls'] = dict(R.STATS)
    tag = '_smoke' if MAX_Q else ''
    (OUT / f'report_v07{tag}.json').write_text(json.dumps(rep, indent=1), encoding='utf-8')
    (OUT / f'results_v07{tag}.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    R.log('REPORT ' + json.dumps(rep, indent=1))


if __name__ == '__main__':
    main()
