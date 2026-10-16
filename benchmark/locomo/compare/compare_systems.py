"""LoCoMo head-to-head: agentmem vs Mem0 vs GBrain vs keyword search, same 200 questions.

Every system gets the same answering model (Muse Spark 1.3) and the same answer prompt; Claude Haiku grades
all four answers for a question together (anonymised, shuffled). agentmem and keyword answers are reused
from the main run (results/results.json); only Mem0 and GBrain are new.

  Mem0   : mem0ai OSS 2.x, its own extraction (Muse as its LLM), fastembed bge-small, local Qdrant. One add per session.
  GBrain : gbrain 0.59, PGLite, hybrid search (keyword + vector + RRF) with bge-small via a local llama-server-style
           endpoint (embed_server.py). No reranker and no LLM query expansion (both need paid APIs).
Context for both is capped at ~4,000 tokens, the same recall budget agentmem gets.

  uv run --no-project --python 3.12 --with "mem0ai[nlp]" --with fastembed --with <en_core_web_sm wheel URL> \
     --with-editable ../../../hypnos python compare_systems.py      (see run_compare.sh)
Needs embed_server.py running on 127.0.0.1:8080 and gbrain on PATH. Every model call is cached, so it resumes.
"""
from __future__ import annotations
import json
import os
import random
import shutil
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault('MEM0_TELEMETRY', 'False')                # no usage stats sent to Mem0's PostHog
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run_locomo as R                                          # noqa: E402
from hypnos.config import Config                              # noqa: E402
from hypnos.llm import OpencodeWriter                         # noqa: E402

WORK = Path(os.environ.get('COMPARE_HOME', r'~\locomo-compare'))
PAGES = WORK / 'pages'
OUT = HERE / 'results'; OUT.mkdir(exist_ok=True)
BUDGET_CHARS = 16000                                            # ~4,000 tokens, same as agentmem's recall budget
ONLY = os.environ.get('ONLY_CONVS')                             # e.g. "0" for a smoke test
MAX_Q = int(os.environ.get('MAX_Q', '0'))                       # limit questions for a smoke test
GBRAIN = shutil.which('gbrain') or str(Path.home() / '.bun' / 'bin' / 'gbrain.exe')
EMBED_URL = 'http://127.0.0.1:8080/v1'
log = R.log


# ---------------------------------------------------------------- Mem0
class MuseLLM:
    """Mem0 LLM adapter: sends Mem0's own system + user prompt to Muse (as an attached file), cached."""
    def __init__(self, writer):
        self.writer = writer

    def generate_response(self, messages, tools=None, tool_choice='auto', **kw):
        body = '\n\n'.join(f'### {m["role"].upper()}\n{m["content"]}' for m in messages)
        return self.writer.complete('Follow the SYSTEM and USER instructions in the attached file exactly. '
                                    'Return only the JSON object they ask for.', [body])


def mem0_memory(i, writer):
    from mem0 import Memory
    home = WORK / 'mem0' / f'conv-{i}'
    home.mkdir(parents=True, exist_ok=True)
    m = Memory.from_config({
        'llm': {'provider': 'openai', 'config': {'api_key': 'unused', 'model': 'muse'}},
        'embedder': {'provider': 'fastembed', 'config': {'model': 'BAAI/bge-small-en-v1.5', 'embedding_dims': 384}},
        'vector_store': {'provider': 'qdrant', 'config': {'path': str(home / 'qdrant'), 'collection_name': f'conv{i}',
                                                          'embedding_model_dims': 384, 'on_disk': True}},
        'history_db_path': str(home / 'history.db'),
    })
    m.llm = MuseLLM(writer)
    return m, home


def build_mem0(i, sample, writer):
    m, home = mem0_memory(i, writer)
    done_f = home / 'done.json'
    done = set(json.loads(done_f.read_text())) if done_f.exists() else set()
    for n, dt, turns in R.sessions(sample['conversation']):
        if n in done:
            continue
        date = (R.parse_date(dt) or '')[:10]
        msgs = [{'role': 'user', 'content': f'[{date}] {R.turn_text(t)}'} for t in turns]
        m.add(msgs, user_id=f'conv{i}', metadata={'session': n, 'date': date})
        done.add(n); done_f.write_text(json.dumps(sorted(done)))
    log(f'conv {i}: mem0 ready ({len(done)} sessions)')
    return m


def mem0_context(m, i, q):
    res = m.search(q, top_k=20, filters={'user_id': f'conv{i}'})
    out, n = [], 0
    for r in res.get('results', []):
        line = f'- ({(r.get("metadata") or {}).get("date", "")}) {r["memory"]}'
        if n + len(line) > BUDGET_CHARS:
            break
        out.append(line); n += len(line)
    return '\n'.join(out)


# ---------------------------------------------------------------- GBrain
def gb_env(i):
    return dict(os.environ, GBRAIN_HOME=str(WORK / 'home' / f'conv-{i}'), LLAMA_SERVER_BASE_URL=EMBED_URL,
                GBRAIN_NO_ONBOARD_NUDGE='1', GBRAIN_NO_AUTOPILOT_INSTALL='1')


def gb(i, *args, timeout=900, check=True):
    p = subprocess.run([GBRAIN, *args], env=gb_env(i), capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout, cwd=str(WORK))
    if check and p.returncode != 0:
        raise RuntimeError(f'gbrain {args[0]} failed: {p.stderr[-400:]}')
    return p.stdout


def build_gbrain(i):
    home = WORK / 'home' / f'conv-{i}'
    if not (home / '.ready').exists():
        home.mkdir(parents=True, exist_ok=True)
        gb(i, 'init', '--pglite', '--non-interactive', '--no-git', '--embedding-model', 'llama-server:bge-small',
           '--embedding-dimensions', '384', check=False)   # exits non-zero over optional skill publishing; import checks the brain
        gb(i, 'import', str(PAGES / f'conv-{i}'))
        gb(i, 'embed', '--stale')
        (home / '.ready').write_text('ok')
    log(f'conv {i}: gbrain ready')


GB_LOCK = defaultdict(threading.Lock)                           # PGLite: one process per brain at a time


def gbrain_context(i, q):
    with GB_LOCK[i]:
        out = gb(i, 'query', q, '--expand', 'false', '--limit', '10', '--json', timeout=300)
    rows = json.loads(out[out.index('['):])
    parts, n = [], 0
    for r in rows:
        block = f'[{r.get("effective_date", "")}] {r.get("title", "")}\n{r.get("chunk_text", "")}'
        if n + len(block) > BUDGET_CHARS:
            block = block[:max(0, BUDGET_CHARS - n)]
        if not block:
            break
        parts.append(block); n += len(block)
        if n >= BUDGET_CHARS:
            break
    return '\n\n'.join(parts)


# ---------------------------------------------------------------- run
def main():
    data = json.loads((R.HERE / 'locomo10.json').read_text(encoding='utf-8'))
    prev = {(r['conv'], r['question']): r for r in json.loads((R.OUT / 'results.json').read_text(encoding='utf-8'))}
    rnd = random.Random(7); pool = defaultdict(list)                   # same 200 questions as run_locomo.py
    for i, s in enumerate(data):
        for q in s['qa']:
            if q.get('category') in R.CATS and 'answer' in q:
                pool[q['category']].append((i, q))
    picked = [x for c in R.CATS for x in rnd.sample(pool[c], min(R.N_PER_CAT, len(pool[c])))]
    if ONLY:
        picked = [x for x in picked if str(x[0]) in ONLY.split(',')]
    if MAX_Q:
        picked = picked[:MAX_Q]
    log(f'{len(picked)} questions')

    cfg = Config(home=R.HERE / '.agentmem', writer='opencode', jev=False, vectors=False)
    writer = R.Cached(OpencodeWriter(cfg), 'muse')
    judge_w = R.Cached(R.HaikuJudge(), 'haiku')
    convs = sorted({i for i, _ in picked})

    for i in convs:                                                     # GBrain builds need no model calls
        build_gbrain(i)
    with ThreadPoolExecutor(R.WORKERS) as ex:
        mems = dict(zip(convs, ex.map(lambda i: build_mem0(i, data[i], writer), convs)))

    def run(item):
        i, q = item
        old = prev[(i, q['question'])]
        ctx = {'mem0': mem0_context(mems[i], i, q['question']), 'gbrain': gbrain_context(i, q['question'])}
        ans = {'agentmem': old['answers']['agentmem'], 'keyword': old['answers']['keyword']}
        ans.update({k: R.answer(writer, data[i], q['question'], c) for k, c in ctx.items()})
        g = R.judge(judge_w, q, ans)
        log(f'[{R.CATS[q["category"]]:<11}] ' + ' '.join(f'{k}={"Y" if v else "n"}' for k, v in g.items()))
        toks = {'agentmem': old['ctx_tokens']['agentmem'], 'keyword': old['ctx_tokens']['keyword'],
                **{k: len(c) // 4 for k, c in ctx.items()}}
        return {'conv': i, 'category': R.CATS[q['category']], 'question': q['question'], 'gold': q['answer'],
                'answers': ans, 'correct': g, 'ctx_tokens': toks}
    with ThreadPoolExecutor(R.WORKERS) as ex:
        res = list(ex.map(run, picked))

    systems = ['agentmem', 'mem0', 'gbrain', 'keyword']
    rep = {'n': len(res), 'judge': 'claude haiku (all 4 answers graded together)', 'answerer': 'muse-spark-1.3',
           'accuracy': {}, 'by_category': {}, 'ctx_tokens': {}, 'not_mentioned': {}}
    for s in systems:
        rep['accuracy'][s] = round(100 * sum(r['correct'][s] for r in res) / len(res), 1)
        rep['ctx_tokens'][s] = round(sum(r['ctx_tokens'][s] for r in res) / len(res))
        rep['not_mentioned'][s] = sum(1 for r in res if r['answers'][s].strip().lower().startswith('not mentioned'))
        rep['by_category'][s] = {c: round(100 * sum(r['correct'][s] for r in res if r['category'] == c) /
                                          max(1, sum(1 for r in res if r['category'] == c)), 1) for c in R.CATS.values()}
    rep['calls'] = dict(R.STATS)
    tag = '_smoke' if (ONLY or MAX_Q) else ''
    (OUT / f'report{tag}.json').write_text(json.dumps(rep, indent=1), encoding='utf-8')
    (OUT / f'results{tag}.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    log('REPORT ' + json.dumps(rep, indent=1))


if __name__ == '__main__':
    main()
