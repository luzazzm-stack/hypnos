"""v0.5 (condition F) = v0.4 (E) + a per-topic ORIGINAL LOG layer.
Zoom: small -> medium -> big -> the topic's own original log (best segments, up to 3,000 tokens) -> whole log only if still not enough.
Reuses E's cached memory; the original log is built by code from the topics' episodes (no LLM calls). All five systems graded together."""
import os, json
from collections import defaultdict
import bench as b, bench_e as e

TOPIC_LOG_TOKENS = 3000
LOG_CHUNK = 1500

def build_F(D, E):
    ep_text = {ep['id']: ep['text'] for ep in D['episodes']}
    chunks, owner = [], []
    for name, t in E['topics'].items():
        eps = sorted({f['id'].split('#')[0] for f in t['facts']})
        for ep in eps:
            txt = ep_text.get(ep, '')
            for i in range(0, len(txt), LOG_CHUNK - 200):
                chunks.append(f'({ep}) ' + txt[i:i + LOG_CHUNK]); owner.append(name)
    b.log(f'F: original log layer = {len(chunks)} segments across {len(E["topics"])} topics')
    return {'log_chunks': chunks, 'log_owner': owner, 'bmLog': b.BM25(chunks)}

def recall_F(E, F, q, chunksA, bmA):
    T = E['topics']
    # 1. keyword search on every layer at once, fuse by topic
    votes = defaultdict(float)
    for r, i in enumerate(E['bmL'].top(q, 15)): votes[E['lvl_docs'][i][0]] += 1 / (60 + r)
    ftop = E['bmF'].top(q, 20)
    for r, i in enumerate(ftop): votes[E['fact_topic'].get(E['facts'][i]['id'], '')] += 1 / (60 + r)
    ltop = F['bmLog'].top(q, 30)
    for r, i in enumerate(ltop[:10]): votes[F['log_owner'][i]] += 1 / (60 + r)
    votes.pop('', None)
    tops = [n for n, _ in sorted(votes.items(), key=lambda x: -x[1])[:2]] or list(T)[:2]
    fb = '\n'.join(f'[{E["facts"][i]["id"]}] {E["facts"][i]["claim"]} | evidence: "{E["facts"][i]["quote"]}"' for i in ftop[:8])
    anc = lambda n: ('\nanchors: ' + ', '.join(T[n]['anchors'])) if T[n]['anchors'] else ''
    summ = lambda lvl: '\n\n'.join(f'[topic {n} / {lvl}] {T[n][lvl]}{anc(n)}' for n in tops)

    def topic_log():
        out, used = [], 0
        for i in ltop:                                   # best-matching segments first, only from the chosen topics
            if F['log_owner'][i] not in tops: continue
            seg = F['log_chunks'][i]
            if used + b.est(seg) > TOPIC_LOG_TOKENS: break
            out.append(seg); used += b.est(seg)
        return '\n\n'.join(out)

    levels = [
        ('small', lambda: summ('small') + '\n\n[facts]\n' + fb),
        ('medium', lambda: summ('medium') + '\n\n[facts]\n' + fb),
        ('big', lambda: summ('big') + '\n\n[facts]\n' + fb),
        ('topic_log', lambda: summ('big') + '\n\n[facts]\n' + fb + '\n\n[original log of these topics]\n' + topic_log()),
        ('whole_log', lambda: summ('big') + '\n\n[facts]\n' + fb + '\n\n[original log of these topics]\n' + topic_log()
                              + '\n\n[whole-history search]\n' + '\n\n'.join(chunksA[i] for i in bmA.top(q, 6))),
    ]
    trace = []
    for lvl, fn in levels:
        ctx = fn()
        if lvl == 'whole_log':
            trace.append({'level': lvl, 'jev_p': None, 'ctx_tokens': b.est(ctx)}); break
        p = (b.jev('gate_f', {'question': q, 'memories': ctx}, e.GATE_E) or {}).get('enough', {}).get('noul')
        trace.append({'level': lvl, 'jev_p': p, 'ctx_tokens': b.est(ctx)})
        if p is not None and p >= 0.85: break
    return ctx, trace, tops

def main():
    sessions = sorted((b.load_session(p) for p in b.SESSIONS), key=lambda s: s['ts'])
    order = [s['id'] for s in sessions]
    qs = json.load(open(os.path.join(b.OUT, 'questions.json'), encoding='utf-8'))
    prevE = {r['q']: r for r in json.load(open(os.path.join(b.OUT, 'results_e.json'), encoding='utf-8'))}
    chunksA, bmA = b.build_A(sessions)
    D = b.sleep_D(sessions)          # cached
    E = e.build_E(D)                 # cached
    F = build_F(D, E)

    def run(q):
        r = prevE[q['q']]
        ctx, trace, tops = recall_F(E, F, q['q'], chunksA, bmA)
        aF = b.answer('F_answer', q['q'], ctx)
        answers = {**r['ans'], 'F': aF}
        g1 = b.grade(q, answers)
        try: g2 = e.grade2(q, answers)
        except Exception as ex: b.log('  grader2 failed:', str(ex)[:120]); g2 = None
        b.log(f'Q done [{" ".join(g1[c][:4] for c in "ABDEF")}] F depth={trace[-1]["level"]}')
        return {**r, 'ans': answers, 'grade5': g1, 'grade5_x': g2, 'traceF': trace, 'topicsF': tops,
                'ctx_tokens': {**r['ctx_tokens'], 'F': b.est(ctx)}}
    res = b.pmap(run, qs)

    sc = lambda g: 1 if g == 'correct' else .5 if g == 'partial' else 0
    rep = {'n': len(res)}
    for key in ('grade5', 'grade5_x'):
        rr = [r for r in res if r.get(key)]
        if not rr: continue
        rep[key] = {'n': len(rr)}
        for c in 'ABDEF':
            gs = [r[key][c] for r in rr]
            rep[key][c] = {'accuracy': round(sum(map(sc, gs)) / len(gs), 3), 'correct': gs.count('correct'), 'partial': gs.count('partial'),
                           'wrong': gs.count('wrong'), 'abstain': gs.count('abstain')}
    rep['by_age'] = {}
    for name, rng in (('oldest', order[:5]), ('middle', order[5:10]), ('newest', order[10:])):
        xs = [r for r in res if r['session'] in rng]
        rep['by_age'][name] = {c: round(100 * sum(sc(r['grade5'][c]) for r in xs) / len(xs)) for c in 'ABDEF'}
    rep['ctx_tokens'] = {c: round(sum(r['ctx_tokens'][c] for r in res) / len(res)) for c in 'ABDEF'}
    depth = defaultdict(list)
    for r in res: depth[r['traceF'][-1]['level']].append((sc(r['grade5']['F']), r['ctx_tokens']['F']))
    rep['F_depth'] = {k: {'n': len(v), 'acc': round(100 * sum(a for a, _ in v) / len(v)), 'ctx': round(sum(t for _, t in v) / len(v))} for k, v in depth.items()}
    if all(r.get('grade5_x') for r in res):
        rep['grader_agreement'] = f'{sum(r["grade5"][c] == r["grade5_x"][c] for r in res for c in "ABDEF")}/{5 * len(res)}'
    rep['stats'] = {k: {**v, 'secs': round(v['secs'], 1)} for k, v in sorted(b.STATS.items()) if k.startswith(('F_', 'jev:gate_f', 'grade'))}
    json.dump(rep, open(os.path.join(b.OUT, 'report_f.json'), 'w', encoding='utf-8'), indent=1)
    json.dump(res, open(os.path.join(b.OUT, 'results_f.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    b.log('REPORT_F', json.dumps(rep, indent=1))

if __name__ == '__main__':
    main()
