"""Condition G: plain opencode, no memory architecture. The 15 session logs sit in ./logs and stock opencode
(build agent, its normal tools) searches them itself to answer each question. Then all six systems are graded together."""
import os, json, time, subprocess
from collections import defaultdict
import bench as b

PLAIN = os.path.join(b.ROOT, 'run_plain'); LOGS = os.path.join(PLAIN, 'logs')

def plain_opencode(q):
    key = b.h('plain', q)
    cp = os.path.join(b.CACHE, key + '.json')
    if os.path.exists(cp):
        return json.load(open(cp, encoding='utf-8'))
    if os.environ.get('CACHE_ONLY'): raise LookupError('not cached: plain')
    prompt = ('The folder ./logs contains the logs of 15 past work sessions between me and an AI assistant, one .txt file per session. '
              'Use your tools to search those files and answer this question: ' + q +
              '\nAnswer briefly and specifically. If the logs do not contain the answer, reply exactly: I don\'t know.')
    env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(b.ROOT, 'cfg'))
    for attempt in range(6):
        t0 = time.time()
        try:
            p = subprocess.run(['opencode', 'run', '-m', b.MODEL, '--format', 'json', '--', prompt], cwd=PLAIN, env=env,
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
        except subprocess.TimeoutExpired:
            b.log('  plain timeout', attempt + 1); time.sleep(60); continue
        texts, steps, tin, tcache, tout, tools, err = [], 0, 0, 0, 0, 0, None
        for line in p.stdout.splitlines():
            try: ev = json.loads(line)
            except Exception: continue
            t = ev.get('type')
            if t == 'text': texts.append(ev['part'].get('text', ''))
            elif t == 'tool_use': tools += 1
            elif t == 'step_finish':
                tk = ev['part'].get('tokens', {}); steps += 1
                tin += tk.get('input', 0); tcache += tk.get('cache', {}).get('read', 0); tout += tk.get('output', 0) + tk.get('reasoning', 0)
            elif t == 'error': err = json.dumps(ev.get('error'))[:300]
        if texts and not err:
            r = {'text': texts[-1].strip(), 'steps': steps, 'tool_calls': tools, 'in': tin, 'cache_read': tcache, 'out': tout, 'secs': time.time() - t0}
            json.dump(r, open(cp, 'w', encoding='utf-8'))
            return r
        b.log(f'  plain retry {attempt+1}: {err or p.stderr[-200:]!r}')
        time.sleep(min(120, 20 * (attempt + 1)))
    raise RuntimeError('plain opencode failed')

def main():
    sessions = sorted((b.load_session(p) for p in b.SESSIONS), key=lambda s: s['ts'])
    order = [s['id'] for s in sessions]
    os.makedirs(LOGS, exist_ok=True)
    for s in sessions:                                   # the same capped logs every other system saw
        open(os.path.join(LOGS, f'session_{s["id"]}.txt'), 'w', encoding='utf-8').write(s['text'])
    prev = {r['q']: r for r in json.load(open(os.path.join(b.OUT, 'results_f.json'), encoding='utf-8'))}
    qs = json.load(open(os.path.join(b.OUT, 'questions.json'), encoding='utf-8'))

    def run(q):
        r = prev[q['q']]
        g = plain_opencode(q['q'])
        answers = {**r['ans'], 'G': g['text']}
        gr = b.grade(q, answers)
        b.log(f'Q done [{" ".join(gr[c][:4] for c in "ABDEFG")}] G steps={g["steps"]} tools={g["tool_calls"]}')
        return {**r, 'ans': answers, 'grade6': gr, 'plain': g}
    def safe(q):
        try: return run(q)
        except LookupError: return None
    res = [r for r in b.pmap(safe, qs) if r]
    b.log(f'{len(res)}/{len(qs)} questions complete')

    sc = lambda g: 1 if g == 'correct' else .5 if g == 'partial' else 0
    rep = {'n': len(res), 'grade6': {}}
    for c in 'ABDEFG':
        gs = [r['grade6'][c] for r in res]
        rep['grade6'][c] = {'accuracy': round(sum(map(sc, gs)) / len(gs), 3), 'correct': gs.count('correct'), 'partial': gs.count('partial'),
                            'wrong': gs.count('wrong'), 'abstain': gs.count('abstain')}
    rep['by_age'] = {}
    for name, rng in (('oldest', order[:5]), ('middle', order[5:10]), ('newest', order[10:])):
        xs = [r for r in res if r['session'] in rng]
        if xs: rep['by_age'][name] = {c: round(100 * sum(sc(r['grade6'][c]) for r in xs) / len(xs)) for c in 'ABDEFG'}
    P = [r['plain'] for r in res]
    n = len(P)
    rep['G_per_question'] = {
        'steps': round(sum(p['steps'] for p in P) / n, 1), 'tool_calls': round(sum(p['tool_calls'] for p in P) / n, 1),
        'input_tokens_total': round(sum(p['in'] + p['cache_read'] for p in P) / n),
        'input_tokens_minus_system_prompt': round(sum(max(0, p['in'] + p['cache_read'] - b.OVERHEAD_IN * p['steps']) for p in P) / n),
        'output_tokens': round(sum(p['out'] for p in P) / n), 'seconds': round(sum(p['secs'] for p in P) / n, 1)}
    json.dump(rep, open(os.path.join(b.OUT, 'report_g.json'), 'w', encoding='utf-8'), indent=1)
    json.dump(res, open(os.path.join(b.OUT, 'results_g.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    b.log('REPORT_G', json.dumps(rep, indent=1))

if __name__ == '__main__':
    main()
