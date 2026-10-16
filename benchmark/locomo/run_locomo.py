"""LoCoMo benchmark for agentmem (standard long-conversation memory benchmark, Maharana et al. 2024).

200 questions stratified over the 4 standard categories (1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop),
across all 10 conversations. Adversarial (5) is excluded, as in most published LoCoMo results.

Systems, all answered by the same model (Muse Spark 1.3 via stock opencode):
  agentmem    : the agentmem engine (architecture v0.6) built from the conversation with sleep
  keyword     : keyword (BM25) search over the raw conversation turns, top 10
  full_context: the whole conversation in the prompt (upper-bound baseline)
Graded by Claude Haiku (`claude -p --model haiku`), a different model family, LoCoMo-J style CORRECT/WRONG.

Every model call is cached in cache/, so the run can stop (free-quota limits) and resume.
    uv run --no-project --python 3.12 --with fastembed --with-editable ../../hypnos python run_locomo.py
"""
from __future__ import annotations
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from hypnos import Memory
from hypnos.config import Config
from hypnos.llm import OpencodeWriter, Writer, parse_json

HERE = Path(__file__).resolve().parent
CACHE = HERE / 'cache'; CACHE.mkdir(exist_ok=True)
OUT = HERE / 'results'; OUT.mkdir(exist_ok=True)
N_PER_CAT = int(os.environ.get('N_PER_CAT', '50'))
WORKERS = int(os.environ.get('WORKERS', '2'))
FULL = os.environ.get('FULL_CONTEXT', '1') == '1'     # the whole-conversation baseline (slow, token heavy)
CATS = {1: 'multi-hop', 2: 'temporal', 3: 'open-domain', 4: 'single-hop'}
STATS = Counter(); LOCK = threading.Lock()
SLOW = threading.Semaphore(int(os.environ.get('PARALLEL_CALLS', '1')))   # how many real model calls may run at once
PAUSE = {'muse': int(os.environ.get('PAUSE_MUSE', '20')), 'haiku': int(os.environ.get('PAUSE_HAIKU', '30'))}


def log(*a):
    with LOCK:
        print(time.strftime('%H:%M:%S'), *a, flush=True)


class Cached(Writer):
    """Caches every writer call on disk by prompt + attachments."""
    def __init__(self, inner, tag):
        self.inner, self.tag = inner, tag

    def complete(self, prompt, attachments=()):
        key = hashlib.sha256(('\x00'.join([self.tag, prompt, *attachments])).encode('utf-8')).hexdigest()[:24]
        p = CACHE / f'{key}.json'
        if p.exists():
            with LOCK: STATS[self.tag + ':cached'] += 1
            return json.loads(p.read_text(encoding='utf-8'))['text']
        with SLOW:                                   # one real model call at a time, then a pause (be gentle on free/plan limits)
            text = self.inner.complete(prompt, attachments)
            p.write_text(json.dumps({'text': text}), encoding='utf-8')
            with LOCK:
                STATS[self.tag + ':calls'] += 1
                STATS[self.tag + ':in_chars'] += len(prompt) + sum(len(a) for a in attachments)
            time.sleep(PAUSE.get(self.tag, 20))
        return text


class HaikuJudge(Writer):
    def complete(self, prompt, attachments=()):
        body = prompt + ''.join(f'\n\n--- attached ---\n{a}' for a in attachments)
        env = dict(os.environ, AGENTMEM_DISABLE='1')
        for attempt in range(4):
            # lean call: tiny system prompt, no tools, no hooks, no MCP, no saved session (~$0.0006 instead of ~$0.026)
            p = subprocess.run(['claude', '-p', '--model', os.environ.get('JUDGE_MODEL', 'claude-haiku-5-5'), '--output-format', 'json',
                                '--system-prompt', 'You grade answers. Reply only with what is asked.', '--tools', '',
                                '--disable-slash-commands', '--no-session-persistence', '--strict-mcp-config',
                                '--settings', '{"disableAllHooks": true}'], input=body, env=env,
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
            try:
                return json.loads(p.stdout)['result']
            except (ValueError, KeyError):
                time.sleep(20 * (attempt + 1))
        raise RuntimeError('haiku judge failed: ' + p.stderr[-300:])


def parse_date(s):
    try:
        return datetime.strptime(s.strip(), '%I:%M %p on %d %B, %Y').strftime('%Y-%m-%dT%H:%M:00Z')
    except ValueError:
        return None


def sessions(conv):
    out = []
    for k, v in conv.items():
        m = re.fullmatch(r'session_(\d+)', k)
        if m and isinstance(v, list):
            out.append((int(m.group(1)), conv.get(f'{k}_date_time', ''), v))
    return sorted(out)


def turn_text(t):
    s = f'{t["speaker"]}: {t["text"]}'
    if t.get('blip_caption'):
        s += f' [shares a photo: {t["blip_caption"]}]'
    return s


def build_memory(i, sample, writer, cfg):
    mem = Memory(f'locomo-{i}', 'chat', cfg=cfg, writer=writer)
    if mem.status()['log_rows'] == 0:
        for n, dt, turns in sessions(sample['conversation']):
            ts = parse_date(dt)
            for t in turns:
                mem.log(turn_text(t), role='user', session=f'session_{n:02d}', meta={'ts': ts, 'dia_id': t['dia_id']}, fast=False)
    while mem.store.unconsolidated():
        rep = mem.sleep(max_episodes=200)
        log(f'conv {i}: sleep {rep}')
    log(f'conv {i}: memory ready {mem.status()}')
    return mem


def full_text(sample):
    parts = []
    for n, dt, turns in sessions(sample['conversation']):
        parts.append(f'=== Session {n} ({dt}) ===\n' + '\n'.join(turn_text(t) for t in turns))
    return '\n\n'.join(parts)


def keyword_context(mem, q, k=10):
    rows = mem.store.search_log(q, k)
    return '\n'.join(f'[{(r["ts"] or "")[:10]}] {r["content"]}' for r in sorted(rows, key=lambda r: r['id']))


def answer(writer, sample, question, context):
    a, b = sample['conversation']['speaker_a'], sample['conversation']['speaker_b']
    return writer.complete(
        f'You are answering a question about a long conversation between {a} and {b}, using only the attached context '
        '(dates in brackets or parentheses are when things were said). Answer with a short phrase, a few words. '
        "For 'when' questions answer with a date, and turn relative times like 'last week' or 'yesterday' into dates using the "
        "dates shown. If the context does not contain the answer, reply exactly: Not mentioned. Question:" + question,
        [context or '(no context)']).strip()


def judge(judge_writer, q, answers):
    labels = list(answers); random.Random(q['question']).shuffle(labels)
    anon = {f'A{i + 1}': answers[l] for i, l in enumerate(labels)}
    txt = judge_writer.complete(
        'You grade answers to a question about a conversation. Question: ' + q['question'] + '\nGold answer: ' + str(q['answer']) +
        '\nFor each candidate answer in the attached JSON, reply CORRECT if it gives the same information as the gold answer '
        '(be generous about wording and length; dates in any format, or relative dates that resolve to the same time, count), '
        'otherwise WRONG. Return ONLY a JSON object mapping each candidate id to "CORRECT" or "WRONG".',
        [json.dumps(anon, ensure_ascii=False, indent=1)])
    res = parse_json(txt) or {}
    return {l: str(res.get(f'A{i + 1}', 'WRONG')).upper() == 'CORRECT' for i, l in enumerate(labels)}


def main():
    data = json.loads((HERE / 'locomo10.json').read_text(encoding='utf-8'))
    rnd = random.Random(7)
    pool = defaultdict(list)
    for i, s in enumerate(data):
        for q in s['qa']:
            if q.get('category') in CATS and 'answer' in q:
                pool[q['category']].append((i, q))
    picked = [x for c in CATS for x in rnd.sample(pool[c], min(N_PER_CAT, len(pool[c])))]
    log(f'{len(picked)} questions: {Counter(CATS[q["category"]] for _, q in picked)}')

    cfg = Config(home=HERE / '.agentmem', writer='opencode', jev=True, vectors=os.environ.get('VECTORS', '1') == '1')
    cfg.spaces_dir.mkdir(parents=True, exist_ok=True)
    muse = OpencodeWriter(cfg)
    writer = Cached(muse, 'muse')
    judge_w = Cached(HaikuJudge(), 'haiku')

    convs = sorted({i for i, _ in picked})
    with ThreadPoolExecutor(WORKERS) as ex:
        mems = dict(zip(convs, ex.map(lambda i: build_memory(i, data[i], writer, cfg), convs)))
    fulls = {i: full_text(data[i]) for i in convs}

    def run(item):
        i, q = item
        mem = mems[i]
        r = mem.recall(q['question'], budget=4000)
        ctx = {'agentmem': r.context, 'keyword': keyword_context(mem, q['question'])}
        if FULL:
            ctx['full_context'] = fulls[i]
        ans = {k: answer(writer, data[i], q['question'], c) for k, c in ctx.items()}
        g = judge(judge_w, q, ans)
        log(f'[{CATS[q["category"]]:<11}] ' + ' '.join(f'{k}={"Y" if v else "n"}' for k, v in g.items()) + f'  level={r.level}')
        return {'conv': i, 'category': CATS[q['category']], 'question': q['question'], 'gold': q['answer'], 'answers': ans,
                'correct': g, 'ctx_tokens': {k: len(c) // 4 for k, c in ctx.items()}, 'agentmem_level': r.level}
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(run, picked))

    systems = ['agentmem', 'keyword'] + (['full_context'] if FULL else [])
    rep = {'n': len(res), 'judge': 'claude haiku', 'answerer': 'muse-spark-1.3', 'accuracy': {}, 'by_category': {}, 'ctx_tokens': {}}
    for s_ in systems:
        rep['accuracy'][s_] = round(100 * sum(r['correct'][s_] for r in res) / len(res), 1)
        rep['ctx_tokens'][s_] = round(sum(r['ctx_tokens'][s_] for r in res) / len(res))
        rep['by_category'][s_] = {c: round(100 * sum(r['correct'][s_] for r in res if r['category'] == c) /
                                           max(1, sum(1 for r in res if r['category'] == c)), 1) for c in CATS.values()}
    rep['agentmem_levels'] = dict(Counter(r['agentmem_level'] for r in res))
    rep['calls'] = dict(STATS)
    (OUT / 'report.json').write_text(json.dumps(rep, indent=1), encoding='utf-8')
    (OUT / 'results.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    log('REPORT ' + json.dumps(rep, indent=1))


if __name__ == '__main__':
    main()
