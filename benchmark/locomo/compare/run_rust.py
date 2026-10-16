"""LoCoMo: the Rust engine (agentmem-rs, v0.8 recall) against the Python engine and the saved head-to-head.

For every question both engines recall from the same stored memories, both warm (Rust as its HTTP service, Python
in-process), and each recall is timed. The Rust context is answered by the same model (Muse Spark 1.3) and graded by
Claude Haiku together with the saved answers of the other systems (Python v0.8, v0.7, v0.6, Mem0, GBrain, keyword).
  SHARD=0/2 uv run --no-project --python 3.12 --with fastembed --with psutil --with-editable ../../../hypnos python run_rust.py
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run_locomo as R                                          # noqa: E402
from hypnos import Memory                                     # noqa: E402
from hypnos.config import Config                              # noqa: E402
from hypnos.llm import OpencodeWriter                         # noqa: E402

OUT = HERE / 'results'
SHARD = os.environ.get('SHARD', '')
MAX_Q = int(os.environ.get('MAX_Q', '0'))
PORT = int(os.environ.get('RUST_PORT', '7501'))
EXE = os.environ.get('AGENTMEM_RUST_EXE') or shutil.which('agentmem-rs') or str(HERE.parent.parent.parent / 'future' / 'agentmem-rs' / 'target' / 'release' / 'agentmem')


def post(path, payload, timeout=180):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', data=json.dumps(payload).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def peak_mb(p):
    mi = p.memory_info()
    return round(getattr(mi, 'peak_wset', 0) / 2**20) if hasattr(mi, 'peak_wset') else round(
        int(next(l.split()[1] for l in open(f'/proc/{p.pid}/status') if l.startswith('VmHWM'))) / 1024)


def main():
    data = json.loads((R.HERE / 'locomo10.json').read_text(encoding='utf-8'))
    prev = json.loads((OUT / 'results_v08.json').read_text(encoding='utf-8'))
    if MAX_Q:
        prev = prev[:MAX_Q]
    if SHARD:
        k, n = map(int, SHARD.split('/'))
        prev = prev[k::n]
    home = R.HERE / '.agentmem'
    env = dict(os.environ, AGENTMEM_HOME=str(home), AGENTMEM_VECTORS='1', AGENTMEM_JEV='1', AGENTMEM_WRITER='fake', AGENTMEM_PORT=str(PORT))
    svc = subprocess.Popen([EXE, 'serve'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if post('/ping', {}, 2).get('ok'):
                    break
            except Exception:
                time.sleep(0.5)
        rust_proc = psutil.Process(svc.pid)
        cfg = Config(home=home, writer='opencode', jev=True, vectors=True)
        writer = R.Cached(OpencodeWriter(cfg), 'muse')
        judge_w = R.Cached(R.HaikuJudge(), 'haiku')
        mems = {i: Memory(f'locomo-{i}', 'chat', cfg=cfg, writer=writer) for i in sorted({x['conv'] for x in prev})}
        for i, m in mems.items():                                          # warm both engines (models loaded)
            m.recall('warm up', 500)
            post('/recall', {'space': f'locomo-{i}', 'profile': 'chat', 'query': 'warm up', 'budget': 500})
        R.log(f'{len(prev)} questions; rust service pid {svc.pid}')

        def run(x):
            i = x['conv']
            q = next(q for q in data[i]['qa'] if q['question'] == x['question'])
            t = time.perf_counter()
            rr = post('/recall', {'space': f'locomo-{i}', 'profile': 'chat', 'query': x['question'], 'budget': 4000})
            t_rs = time.perf_counter() - t
            t = time.perf_counter()
            pr = mems[i].recall(x['question'], 4000)
            t_py = time.perf_counter() - t
            ans = dict(x['answers'])
            ans['agentmem_v08_rust'] = R.answer(writer, data[i], x['question'], rr['context'])
            g = R.judge(judge_w, q, ans)
            R.log(f'[{x["category"]:<11}] rust={"Y" if g["agentmem_v08_rust"] else "n"} py={"Y" if g["agentmem_v08"] else "n"}  '
                  f'rust {t_rs:.1f}s {rr["tokens"]}t | py {t_py:.1f}s {pr.tokens}t')
            return {**x, 'answers': ans, 'correct': g, 'ctx_tokens': {**x['ctx_tokens'], 'agentmem_v08_rust': rr['tokens']},
                    'recall_seconds': {'rust': round(t_rs, 2), 'python': round(t_py, 2)},
                    'python_rerun_tokens': pr.tokens, 'rust_level': rr['level']}
        with ThreadPoolExecutor(R.WORKERS) as ex:
            res = list(ex.map(run, prev))
        ram = {'rust_service_peak_mb': peak_mb(rust_proc), 'python_process_peak_mb': peak_mb(psutil.Process())}
    finally:
        svc.terminate()
    tag = '_smoke' if MAX_Q else (f"_shard{SHARD.split('/')[0]}" if SHARD else '')
    (OUT / f'results_rust{tag}.json').write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding='utf-8')
    (OUT / f'ram_rust{tag}.json').write_text(json.dumps(ram), encoding='utf-8')
    R.log('DONE ' + json.dumps(ram))


if __name__ == '__main__':
    main()
