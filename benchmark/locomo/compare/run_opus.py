"""LoCoMo with a strong answering model: Claude Opus 5.5 (effort medium) answers from each system's context.

Same 200 questions. Contexts are rebuilt locally (no answering-model cost): agentmem v0.8 (Jev-routed recall), Mem0
(search), GBrain (hybrid query) and keyword search. Opus 5.5 answers every context with the same prompt; Claude Haiku
grades all four answers to each question together. Every Opus call's real token usage is logged to results/opus_usage.jsonl.
Needs: embed_server.py on 127.0.0.1:8080 and gbrain on PATH (for GBrain), the Mem0 stores in ~\\locomo-compare.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('MEM0_TELEMETRY', 'False')
import run_locomo as R                                          # noqa: E402
import compare_systems as C                                     # noqa: E402
from hypnos import Memory                                     # noqa: E402
from hypnos.config import Config                              # noqa: E402
from hypnos.llm import Writer                                 # noqa: E402

OUT = HERE / 'results'
MAX_Q = int(os.environ.get('MAX_Q', '0'))
USAGE = OUT / ('opus_usage_smoke.jsonl' if MAX_Q else 'opus_usage.jsonl')
ULOCK = threading.Lock()
SYSTEMS = ['agentmem_v08', 'mem0', 'gbrain', 'keyword']


class OpusWriter(Writer):
    """Claude Opus 5.5 at medium effort through `claude -p` with a minimal system prompt, no tools, hooks or MCP."""
    def complete(self, prompt, attachments=()):
        body = prompt + ''.join(f'\n\n--- attached context ---\n{a}' for a in attachments)
        env = dict(os.environ, AGENTMEM_DISABLE='1')
        for attempt in range(4):
            p = subprocess.run(['claude', '-p', '--model', 'claude-opus-5-5', '--effort', 'medium', '--output-format', 'json',
                                '--system-prompt', 'You answer questions about a conversation from the attached context. Reply only with the answer.',
                                '--tools', '', '--disable-slash-commands', '--no-session-persistence', '--strict-mcp-config',
                                '--settings', '{"disableAllHooks": true}'], input=body, env=env, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=600)
            try:
                d = json.loads(p.stdout)
                if d.get('is_error'):
                    raise ValueError(d.get('result'))
                u = d.get('usage', {})
                with ULOCK, USAGE.open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'in': u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0),
                                        'out': u.get('output_tokens', 0), 'thinking': (u.get('output_tokens_details') or {}).get('thinking_tokens', 0),
                                        'usd': d.get('total_cost_usd', 0)}) + '\n')
                if not (d.get('result') or '').strip():                  # an empty reply is a failure, not an answer
                    raise ValueError('empty reply')
                return d['result'].strip()
            except (ValueError, KeyError):
                time.sleep(30 * (attempt + 1))
        raise RuntimeError('opus answer failed: ' + (p.stderr or p.stdout)[-300:])


def main():
    data = json.loads((R.HERE / 'locomo10.json').read_text(encoding='utf-8'))
    prev = json.loads((OUT / 'results_v08.json').read_text(encoding='utf-8'))
    if MAX_Q:
        prev = prev[:MAX_Q]
    cfg = Config(home=R.HERE / '.agentmem', writer='fake', jev=True, vectors=True)
    opus = R.Cached(OpusWriter(), 'opus')
    judge_w = R.Cached(R.HaikuJudge(), 'haiku')
    convs = sorted({x['conv'] for x in prev})
    mems = {i: Memory(f'locomo-{i}', 'chat', cfg=cfg) for i in convs}
    mem0 = {i: C.mem0_memory(i, None)[0] for i in convs}
    R.log(f'{len(prev)} questions, {len(SYSTEMS)} systems, answered by claude-opus-5-5 (medium)')

    def run(x):
        i = x['conv']
        q = next(q for q in data[i]['qa'] if q['question'] == x['question'])
        ctx = {'agentmem_v08': mems[i].recall(x['question'], 4000).context,
               'mem0': C.mem0_context(mem0[i], i, x['question']),
               'gbrain': C.gbrain_context(i, x['question']),
               'keyword': R.keyword_context(mems[i], x['question'])}
        ans = {k: R.answer(opus, data[i], x['question'], c) for k, c in ctx.items()}
        g = R.judge(judge_w, q, ans)
        R.log(f'[{x["category"]:<11}] ' + ' '.join(f'{k}={"Y" if v else "n"}' for k, v in g.items()))
        return {'conv': i, 'category': x['category'], 'question': x['question'], 'gold': x['gold'], 'answers': ans, 'correct': g,
                'ctx_tokens': {k: len(c) // 4 for k, c in ctx.items()}}
    with ThreadPoolExecutor(R.WORKERS) as ex:
        res = list(ex.map(run, prev))

    use = [json.loads(l) for l in USAGE.read_text(encoding='utf-8').splitlines()] if USAGE.exists() else []
    rep = {'n': len(res), 'answerer': 'claude-opus-5-5 (effort medium)', 'judge': 'claude haiku (all 4 answers graded together)',
           'accuracy': {}, 'by_category': {}, 'ctx_tokens': {}, 'not_mentioned': {},
           'opus_usage': {'calls': len(use), 'input_tokens': sum(u['in'] for u in use), 'output_tokens': sum(u['out'] for u in use),
                          'thinking_tokens': sum(u['thinking'] for u in use), 'usd_list_price': round(sum(u['usd'] for u in use), 2)}}
    for s in SYSTEMS:
        rep['accuracy'][s] = round(100 * sum(r['correct'][s] for r in res) / len(res), 1)
        rep['ctx_tokens'][s] = round(sum(r['ctx_tokens'][s] for r in res) / len(res))
        rep['not_mentioned'][s] = sum(1 for r in res if r['answers'][s].strip().lower().startswith('not mentioned'))
        rep['by_category'][s] = {c: round(100 * sum(r['correct'][s] for r in res if r['category'] == c) /
                                          max(1, sum(1 for r in res if r['category'] == c)), 1) for c in R.CATS.values()}
    tag = '_smoke' if MAX_Q else ''
    (OUT / f'report_opus{tag}.json').write_text(json.dumps(rep, indent=1), encoding='utf-8')
    (OUT / f'results_opus{tag}.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    R.log('REPORT ' + json.dumps(rep, indent=1))


if __name__ == '__main__':
    main()
