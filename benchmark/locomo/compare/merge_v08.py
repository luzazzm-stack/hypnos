"""Merge the v0.8 shard results (two machines) into results_v08.json and report_v08.json."""
import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent / 'results'
CATS = ['multi-hop', 'temporal', 'open-domain', 'single-hop']
res = []
for f in sorted(OUT.glob('results_v08_shard*.json')):
    res += json.loads(f.read_text(encoding='utf-8'))
systems = ['agentmem_v08', 'agentmem_v07', 'mem0', 'gbrain', 'agentmem', 'keyword']
rep = {'n': len(res), 'judge': 'claude haiku (all 6 answers graded together)', 'answerer': 'muse-spark-1.3',
       'accuracy': {}, 'by_category': {}, 'ctx_tokens': {}, 'not_mentioned': {},
       'v08_levels': dict(Counter(r['v08_level'] for r in res))}
for s in systems:
    rep['accuracy'][s] = round(100 * sum(r['correct'][s] for r in res) / len(res), 1)
    rep['ctx_tokens'][s] = round(sum(r['ctx_tokens'][s] for r in res) / len(res))
    rep['not_mentioned'][s] = sum(1 for r in res if r['answers'][s].strip().lower().startswith('not mentioned'))
    rep['by_category'][s] = {c: round(100 * sum(r['correct'][s] for r in res if r['category'] == c) /
                                      max(1, sum(1 for r in res if r['category'] == c)), 1) for c in CATS}
(OUT / 'results_v08.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
(OUT / 'report_v08.json').write_text(json.dumps(rep, indent=1), encoding='utf-8')
print(json.dumps(rep, indent=1))
