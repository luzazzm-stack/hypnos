"""v0.6 (condition H) = v0.5 (F) plus:
 1. exact-value rule: Jev flags questions that ask for an exact value; those may only stop on EVIDENCE (fact quotes, anchors, original log)
 2. the gate judges evidence only for exact-value questions (a fluent summary can't pass it)
 3. exact-value questions get the top original-log snippets attached from the first step
 4. answer prompt: quote exact values from evidence/log, full paths and sources
 5. vector search (local bge-small) fused with keyword search on every layer
 6. log steps drop the big summaries (facts + log only)
Runs with:  uv run --no-project --python 3.12 --with fastembed python bench_h.py"""
import os, json, hashlib
from collections import defaultdict
import numpy as np
import bench as b, bench_e as e, bench_f as f

TOPIC_LOG_TOKENS = 3000
QPREFIX = 'Represent this sentence for searching relevant passages: '
_model = None

def embed(texts, query=False):
    global _model
    key = hashlib.sha256(('q' if query else 'd').encode() + '\x00'.join(texts).encode('utf-8')).hexdigest()[:24]
    cp = os.path.join(b.CACHE, f'emb_{key}.npy')
    if os.path.exists(cp): return np.load(cp)
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding('BAAI/bge-small-en-v1.5', threads=2)   # low-RAM laptop: 2 threads
    # small batches keep peak RAM low (this laptop has 7.5 GB)
    v = np.array(list(_model.embed([(QPREFIX + t) if query else t[:1200] for t in texts], batch_size=8)), dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    np.save(cp, v)
    return v

class Hybrid:
    """keyword (BM25) + vector search, fused by reciprocal rank"""
    def __init__(self, docs):
        self.bm = b.BM25(docs); self.vec = embed(docs)
    def top(self, q, k, qv):
        kw = self.bm.top(q, 50)
        vs = list(np.argsort(-(self.vec @ qv))[:50])
        score = defaultdict(float)
        for r, i in enumerate(kw): score[i] += 1 / (60 + r)
        for r, i in enumerate(vs): score[int(i)] += 1 / (60 + r)
        return [i for i, _ in sorted(score.items(), key=lambda x: -x[1])[:k]]

EXACT_Q = {'exact': {'type': 'noul',
                     'instructions': 'Does the question in `question` ask for a specific exact value that must be reproduced precisely, '
                                     'such as a name, file path, number, ID, setting, version, command or quoted output string?',
                     'criteria': {'true': 'The correct answer is a precise value that must match exactly.',
                                  'false': 'The question asks for an explanation, reason, summary or general description.'}}}
GATE_EVIDENCE = {'enough': {'type': 'noul',
                            'instructions': 'Does the evidence in `evidence` contain, word for word, the exact information the question in `question` asks for?',
                            'criteria': {'true': 'The exact value asked for appears verbatim in the evidence.',
                                         'false': 'The exact value is missing, incomplete, or only paraphrased.'}}}

def build_H(D, E, F, chunksA):
    lvl_docs = E['lvl_docs']; T = E['topics']
    H = {
        'L': Hybrid([f'{n.replace("_", " ")} {T[n]["desc"]} {T[n][lvl]} {" ".join(T[n]["anchors"])}' for n, lvl in lvl_docs]),
        'F': Hybrid([x['claim'] + ' ' + x.get('quote', '') for x in E['facts']]),
        'Log': Hybrid(F['log_chunks']),
        'Raw': Hybrid(chunksA),
    }
    b.log('H: hybrid keyword + vector indexes ready')
    return H

def recall_H(E, F, H, q, chunksA):
    T = E['topics']; qv = embed([q], query=True)[0]
    exact = (b.jev('exact', {'question': q}, EXACT_Q) or {}).get('exact', {}).get('noul', 0.5) >= 0.5
    votes = defaultdict(float)
    for r, i in enumerate(H['L'].top(q, 15, qv)): votes[E['lvl_docs'][i][0]] += 1 / (60 + r)
    ftop = H['F'].top(q, 20, qv)
    for r, i in enumerate(ftop): votes[E['fact_topic'].get(E['facts'][i]['id'], '')] += 1 / (60 + r)
    ltop = H['Log'].top(q, 30, qv)
    for r, i in enumerate(ltop[:10]): votes[F['log_owner'][i]] += 1 / (60 + r)
    votes.pop('', None)
    tops = [n for n, _ in sorted(votes.items(), key=lambda x: -x[1])[:2]] or list(T)[:2]
    fb = '\n'.join(f'[{E["facts"][i]["id"]}] {E["facts"][i]["claim"]} | evidence: "{E["facts"][i]["quote"]}"' for i in ftop[:8])
    anc = lambda n: ('\nanchors: ' + ', '.join(T[n]['anchors'])) if T[n]['anchors'] else ''
    summ = lambda lvl: '\n\n'.join(f'[topic {n} / {lvl}] {T[n][lvl]}{anc(n)}' for n in tops)
    def tlog(budget):
        out, used = [], 0
        for i in ltop:
            if F['log_owner'][i] not in tops: continue
            seg = F['log_chunks'][i]
            if used + b.est(seg) > budget: break
            out.append(seg); used += b.est(seg)
        return '\n\n'.join(out)
    snip = ('\n\n[original log snippets]\n' + tlog(800)) if exact else ''
    ev_small = '[facts]\n' + fb + snip
    levels = [
        ('small', summ('small'), ev_small),
        ('medium', summ('medium'), ev_small),
        ('big', summ('big'), ev_small),
        ('topic_log', summ('small'), '[facts]\n' + fb + '\n\n[original log of these topics]\n' + tlog(TOPIC_LOG_TOKENS)),
        ('whole_log', summ('small'), '[facts]\n' + fb + '\n\n[original log of these topics]\n' + tlog(TOPIC_LOG_TOKENS)
                                      + '\n\n[whole-history search]\n' + '\n\n'.join(chunksA[i] for i in H['Raw'].top(q, 6, qv))),
    ]
    trace = []
    for lvl, orient, evidence in levels:
        ctx = orient + '\n\n' + evidence
        if lvl == 'whole_log':
            trace.append({'level': lvl, 'jev_p': None, 'ctx_tokens': b.est(ctx)}); break
        if exact:   # 1+2: exact-value questions may only stop on evidence, and the gate sees evidence only
            p = (b.jev('gate_h_ev', {'question': q, 'evidence': evidence}, GATE_EVIDENCE) or {}).get('enough', {}).get('noul')
        else:
            p = (b.jev('gate_h', {'question': q, 'memories': ctx}, e.GATE_E) or {}).get('enough', {}).get('noul')
        trace.append({'level': lvl, 'jev_p': p, 'ctx_tokens': b.est(ctx)})
        if p is not None and p >= 0.85: break
    return ctx, trace, tops, exact

def answer_H(q, ctx):
    return b.muse('H_answer', (
        'You are an AI assistant answering from your memory of past sessions with this user. Your memory is in the attached file: '
        'topic summaries (paraphrased, for orientation), verified facts with exact evidence quotes, and original log text. '
        'Question: ' + q + '\nAnswer briefly and specifically using only the memory. When the question asks for an exact value '
        '(name, path, number, ID, setting, version, command or output string), copy it character for character from the evidence quotes '
        'or original log, not from a summary, and include the full path, source or location if the memory gives one. '
        'If the memory does not contain the answer, reply exactly: I don\'t know.'), [ctx])

def main():
    sessions = sorted((b.load_session(p) for p in b.SESSIONS), key=lambda s: s['ts'])
    order = [s['id'] for s in sessions]
    qs = json.load(open(os.path.join(b.OUT, 'questions.json'), encoding='utf-8'))
    prevF = {r['q']: r for r in json.load(open(os.path.join(b.OUT, 'results_f.json'), encoding='utf-8'))}
    chunksA, bmA = b.build_A(sessions)
    D = b.sleep_D(sessions); E = e.build_E(D); F = f.build_F(D, E)     # all cached
    H = build_H(D, E, F, chunksA)

    def run(q):
        r = prevF[q['q']]
        ctx, trace, tops, exact = recall_H(E, F, H, q['q'], chunksA)
        aH = answer_H(q['q'], ctx)
        answers = {**r['ans'], 'H': aH}
        g1 = b.grade(q, answers)
        g2 = None
        if not os.environ.get('NO_GRADER2'):          # Muse Spark 1.2 cross-check (skipped when its server is failing)
            try: g2 = e.grade2(q, answers)
            except Exception as ex: b.log('  grader2 failed:', str(ex)[:120])
        b.log(f'Q done [{" ".join(g1[c][:4] for c in "ABDEFH")}] H exact={exact} depth={trace[-1]["level"]}')
        return {**r, 'ans': answers, 'grade6': g1, 'grade6_x': g2, 'traceH': trace, 'topicsH': tops, 'exactH': exact,
                'ctx_tokens': {**r['ctx_tokens'], 'H': b.est(ctx)}}
    def safe(q):
        try: return run(q)
        except LookupError: return None
    res = [r for r in b.pmap(safe, qs) if r]
    b.log(f'{len(res)}/{len(qs)} questions complete')

    sc = lambda g: 1 if g == 'correct' else .5 if g == 'partial' else 0
    rep = {'n': len(res)}
    for key in ('grade6', 'grade6_x'):
        rr = [r for r in res if r.get(key)]
        if not rr: continue
        rep[key] = {'n': len(rr)}
        for c in 'ABDEFH':
            gs = [r[key][c] for r in rr]
            rep[key][c] = {'accuracy': round(sum(map(sc, gs)) / len(gs), 3), 'correct': gs.count('correct'), 'partial': gs.count('partial'),
                           'wrong': gs.count('wrong'), 'abstain': gs.count('abstain')}
    rep['by_age'] = {}
    for name, rng in (('oldest', order[:5]), ('middle', order[5:10]), ('newest', order[10:])):
        xs = [r for r in res if r['session'] in rng]
        if xs: rep['by_age'][name] = {c: round(100 * sum(sc(r['grade6'][c]) for r in xs) / len(xs)) for c in 'ABDEFH'}
    rep['ctx_tokens'] = {c: round(sum(r['ctx_tokens'][c] for r in res) / len(res)) for c in 'ABDEFH'}
    depth = defaultdict(list)
    for r in res: depth[r['traceH'][-1]['level']].append((sc(r['grade6']['H']), r['ctx_tokens']['H']))
    rep['H_depth'] = {k: {'n': len(v), 'acc': round(100 * sum(a for a, _ in v) / len(v)), 'ctx': round(sum(t for _, t in v) / len(v))} for k, v in depth.items()}
    ex = [r for r in res if r['exactH']]
    rep['H_exact_questions'] = {'n': len(ex), 'acc': round(100 * sum(sc(r['grade6']['H']) for r in ex) / max(1, len(ex))),
                                'F_acc_same': round(100 * sum(sc(r['grade6']['F']) for r in ex) / max(1, len(ex)))}
    if all(r.get('grade6_x') for r in res):
        rep['grader_agreement'] = f'{sum(r["grade6"][c] == r["grade6_x"][c] for r in res for c in "ABDEFH")}/{6 * len(res)}'
    rep['stats'] = {k: {**v, 'secs': round(v['secs'], 1)} for k, v in sorted(b.STATS.items()) if k.startswith(('H_', 'jev:exact', 'jev:gate_h', 'grade'))}
    json.dump(rep, open(os.path.join(b.OUT, 'report_h.json'), 'w', encoding='utf-8'), indent=1)
    json.dump(res, open(os.path.join(b.OUT, 'results_h.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    b.log('REPORT_H', json.dumps(rep, indent=1))

if __name__ == '__main__':
    main()
