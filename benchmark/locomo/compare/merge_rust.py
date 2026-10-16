"""Merge the Rust-engine LoCoMo shards into report_rust.json: accuracy, context, speed and RAM, Rust vs Python."""
import json
from pathlib import Path
from statistics import mean, median

OUT = Path(__file__).resolve().parent / 'results'
CATS = ['multi-hop', 'temporal', 'open-domain', 'single-hop']
res = []
for f in sorted(OUT.glob('results_rust_shard*.json')):
    res += json.loads(f.read_text(encoding='utf-8'))
rams = [json.loads(f.read_text()) for f in sorted(OUT.glob('ram_rust_shard*.json'))]
systems = ['agentmem_v08_rust', 'agentmem_v08', 'agentmem_v07', 'mem0', 'gbrain', 'agentmem', 'keyword']
rep = {'n': len(res), 'judge': 'claude haiku (all 7 answers graded together)', 'answerer': 'muse-spark-1.3',
       'accuracy': {}, 'by_category': {}, 'ctx_tokens': {}, 'not_mentioned': {}}
for s in systems:
    rep['accuracy'][s] = round(100 * sum(r['correct'][s] for r in res) / len(res), 1)
    rep['ctx_tokens'][s] = round(sum(r['ctx_tokens'][s] for r in res) / len(res))
    rep['not_mentioned'][s] = sum(1 for r in res if r['answers'][s].strip().lower().startswith('not mentioned'))
    rep['by_category'][s] = {c: round(100 * sum(r['correct'][s] for r in res if r['category'] == c) /
                                      max(1, sum(1 for r in res if r['category'] == c)), 1) for c in CATS}
for eng in ('rust', 'python'):
    ts = [r['recall_seconds'][eng] for r in res]
    rep[f'recall_seconds_{eng}'] = {'mean': round(mean(ts), 2), 'median': round(median(ts), 2), 'p90': round(sorted(ts)[int(0.9 * len(ts))], 2)}
rep['ram_mb'] = rams
(OUT / 'results_rust.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
(OUT / 'report_rust.json').write_text(json.dumps(rep, indent=1), encoding='utf-8')
print(json.dumps(rep, indent=1))
