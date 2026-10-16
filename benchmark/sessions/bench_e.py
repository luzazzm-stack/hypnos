"""v0.4 (condition E) on top of the cached A/B/D run.
E = D's memory + keyword anchors on every summary, topic splitting, parallel keyword search over all summary levels,
facts and raw log (rank fusion), a sharper Jev gate, and a 6-chunk raw fallback. All four systems are graded together."""
import os, re, json, random
from collections import Counter, defaultdict
import bench as b

SPLIT_FACTS = 24          # a topic with more verified facts than this is split into chronological parts
GRADER2 = 'opencode/muse-spark-1.2-contributor-free'   # cross-check grader (different model)

ANCHOR_RE = re.compile(
    r'`([^`]{2,60})`'                                   # backticked
    r'|([A-Za-z]:\\[^\s"\'|,;)]+|~?/[\w.\-/]{3,}[\w])'    # windows / unix paths
    r'|((?<!\w)--[a-z][\w-]{2,})'                       # cli flags (double dash only)
    r'|(\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b)'          # IPs
    r'|(\b[\w-]+\.(?:ps1|py|json|md|ini|cfg|exe|bat|sh|html|js|ts|yaml|yml|toml|apk|log|txt|zip|7z)\b)'
    r'|(\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b)'  # camelCase / PascalCase with an inner capital
    r'|(\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b)'                # snake_case and IDs like VID_2CA3
    r'|(\b[\w.-]*\d[\w.-]*\d[\w.-]*\b)')                 # tokens with 2+ digits (versions, ids, values)

def anchors_for(facts, cap=30):
    seen, out = set(), []
    for f in facts:
        for m in ANCHOR_RE.finditer(f['claim'] + ' ' + f.get('quote', '')):
            a = next(g for g in m.groups() if g).strip().strip('.,')
            if len(a) < 3 or a.lower() in seen: continue
            seen.add(a.lower()); out.append(a)
    return out[:cap]

def build_E(D):
    fact_topic = {}
    for ep in D['episodes']:
        for f in ep['facts']:
            if f.get('muse_ok') and ep.get('topic'): fact_topic[f['id']] = ep['topic']
    topics = {}
    split_jobs = []
    for name, t in D['topics'].items():
        tf = t['facts']
        if len(tf) <= SPLIT_FACTS:
            topics[name] = {**{k: t[k] for k in ('desc', 'small', 'medium', 'big')}, 'facts': tf}
            continue
        parts, cur = [], []
        for ep_id in t['episodes']:                    # episodes are already in chronological order
            ef = [f for f in tf if f['id'].startswith(ep_id + '#')]
            if cur and len(cur) + len(ef) > SPLIT_FACTS: parts.append(cur); cur = []
            cur += ef
        if cur: parts.append(cur)
        for i, pf in enumerate(parts, 1):
            pname = f'{name}__p{i}'
            topics[pname] = {'desc': f'{t["desc"]} (part {i} of {len(parts)}, oldest first)', 'facts': pf}
            for f in pf: fact_topic[f['id']] = pname
            for size in ('small', 'medium', 'big'): split_jobs.append((pname, size, pf))
    SIZES = {'small': (60, 'one or two sentences'), 'medium': (300, 'a short paragraph or a few bullets'), 'big': (1500, 'a detailed structured summary')}
    def write(job):
        name, size, tf = job; tokens, shape = SIZES[size]
        return b.muse('E_split_topics', (
            f'Write the {size.upper()} memory summary for the topic "{name}" from the verified facts in the attached file. '
            f'Size: about {tokens} tokens, {shape}. Keep the most useful specifics (values, paths, commands, decisions, outcomes). '
            'Use only these facts. Return only the summary text.'),
            ['\n'.join(f'[{f["id"]}] ({f["kind"]}) {f["claim"]}' for f in tf)])
    for (name, size, _), txt in zip(split_jobs, b.pmap(write, split_jobs)): topics[name][size] = txt
    for t in topics.values(): t['anchors'] = anchors_for(t['facts'])
    b.log(f'E: {len(topics)} topics after splitting ({len(split_jobs)//3} split parts), anchors avg {sum(len(t["anchors"]) for t in topics.values())/len(topics):.1f}')
    names = list(topics)
    lvl_docs = [(n, lvl) for n in names for lvl in ('small', 'medium', 'big')]
    bmL = b.BM25([f'{n.replace("_", " ")} {topics[n]["desc"]} {topics[n][lvl]} {" ".join(topics[n]["anchors"])}' for n, lvl in lvl_docs])
    facts = [f for t in topics.values() for f in t['facts']]
    bmF = b.BM25([f['claim'] + ' ' + f.get('quote', '') for f in facts])
    return {'topics': topics, 'lvl_docs': lvl_docs, 'bmL': bmL, 'facts': facts, 'bmF': bmF, 'fact_topic': fact_topic}

GATE_E = {'enough': {'type': 'noul',
                     'instructions': 'Do the memories in `memories` contain the specific information the question in `question` asks for '
                                     '(the exact value, name, path, command, setting or reason), so it can be answered precisely?',
                     'criteria': {'true': 'The exact information asked for appears in the memories.',
                                  'false': 'The memories are on the right subject but the exact information asked for is missing or only described vaguely.'}}}

def recall_E(E, q, chunksA, bmA):
    T = E['topics']
    votes = defaultdict(float)
    for r, i in enumerate(E['bmL'].top(q, 15)): votes[E['lvl_docs'][i][0]] += 1 / (60 + r)
    ftop = E['bmF'].top(q, 20)
    for r, i in enumerate(ftop): votes[E['fact_topic'].get(E['facts'][i]['id'], '')] += 1 / (60 + r)
    votes.pop('', None)
    tops = [n for n, _ in sorted(votes.items(), key=lambda x: -x[1])[:2]] or list(T)[:2]
    fb = '\n'.join(f'[{E["facts"][i]["id"]}] {E["facts"][i]["claim"]} | evidence: "{E["facts"][i]["quote"]}"' for i in ftop[:8])
    anc = lambda n: ('\nanchors: ' + ', '.join(T[n]['anchors'])) if T[n]['anchors'] else ''
    raw = lambda k: '\n\n'.join(chunksA[i] for i in bmA.top(q, k))
    levels = [
        ('small', lambda: '\n\n'.join(f'[topic {n} / small] {T[n]["small"]}{anc(n)}' for n in tops) + '\n\n[facts]\n' + fb),
        ('medium+raw3', lambda: '\n\n'.join(f'[topic {n} / medium] {T[n]["medium"]}{anc(n)}' for n in tops) + '\n\n[facts]\n' + fb + '\n\n[raw log]\n' + raw(3)),
        ('big+raw6', lambda: '\n\n'.join(f'[topic {n} / big] {T[n]["big"]}{anc(n)}' for n in tops) + '\n\n[facts]\n' + fb + '\n\n[raw log]\n' + raw(6)),
    ]
    trace = []
    for lvl, fn in levels:
        ctx = fn()
        p = (b.jev('gate_e', {'question': q, 'memories': ctx}, GATE_E) or {}).get('enough', {}).get('noul')
        trace.append({'level': lvl, 'jev_p': p, 'ctx_tokens': b.est(ctx)})
        if lvl != 'big+raw6' and p is not None and p >= 0.85: break
    return ctx, trace, tops

def main():
    sessions = sorted((b.load_session(p) for p in b.SESSIONS), key=lambda s: s['ts'])
    order = [s['id'] for s in sessions]
    qs = json.load(open(os.path.join(b.OUT, 'questions.json'), encoding='utf-8'))
    prev = {r['q']: r for r in json.load(open(os.path.join(b.OUT, 'results.json'), encoding='utf-8'))}
    chunksA, bmA = b.build_A(sessions)
    D = b.sleep_D(sessions)                 # cached
    E = build_E(D)

    def run(q):
        r = prev[q['q']]
        ctx, trace, tops = recall_E(E, q['q'], chunksA, bmA)
        aE = b.answer('E_answer', q['q'], ctx)
        answers = {**r['ans'], 'E': aE}
        g1 = b.grade(q, answers)
        try:
            g2 = grade2(q, answers)
        except Exception as ex:
            b.log('  grader2 failed:', str(ex)[:120]); g2 = None
        b.log(f'Q done [{g1["A"][:4]} {g1["B"][:4]} {g1["D"][:4]} {g1["E"][:4]}] E depth={trace[-1]["level"]}')
        return {**r, 'ans': answers, 'grade4': g1, 'grade_x': g2, 'traceE': trace, 'topicsE': tops,
                'ctx_tokens': {**r['ctx_tokens'], 'E': b.est(ctx)}}
    res = b.pmap(run, qs)

    sc = lambda g: 1 if g == 'correct' else .5 if g == 'partial' else 0
    rep = {'n': len(res)}
    for key in ('grade4', 'grade_x'):
        rr = [r for r in res if r.get(key)]
        if not rr: continue
        rep[key] = {'n': len(rr)}
        for c in 'ABDE':
            gs = [r[key][c] for r in rr]
            rep[key][c] = {'accuracy': round(sum(map(sc, gs)) / len(gs), 3), 'correct': gs.count('correct'), 'partial': gs.count('partial'),
                           'wrong': gs.count('wrong'), 'abstain': gs.count('abstain')}
    rep['by_age'] = {}
    for name, rng in (('oldest', order[:5]), ('middle', order[5:10]), ('newest', order[10:])):
        xs = [r for r in res if r['session'] in rng]
        rep['by_age'][name] = {c: round(100 * sum(sc(r['grade4'][c]) for r in xs) / len(xs)) for c in 'ABDE'}
    rep['ctx_tokens'] = {c: round(sum(r['ctx_tokens'][c] for r in res) / len(res)) for c in 'ABDE'}
    depth = defaultdict(list)
    for r in res: depth[r['traceE'][-1]['level']].append((sc(r['grade4']['E']), r['ctx_tokens']['E']))
    rep['E_depth'] = {k: {'n': len(v), 'acc': round(100 * sum(a for a, _ in v) / len(v)), 'ctx': round(sum(t for _, t in v) / len(v))} for k, v in depth.items()}
    if all(r.get('grade_x') for r in res):
        agree = sum(r['grade4'][c] == r['grade_x'][c] for r in res for c in 'ABDE')
        rep['grader_agreement'] = f'{agree}/{4 * len(res)}'
    rep['stats'] = {k: {**v, 'secs': round(v['secs'], 1)} for k, v in sorted(b.STATS.items()) if k.startswith(('E_', 'jev:gate_e', 'grade'))}
    json.dump(rep, open(os.path.join(b.OUT, 'report_e.json'), 'w', encoding='utf-8'), indent=1)
    json.dump(res, open(os.path.join(b.OUT, 'results_e.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    b.log('REPORT_E', json.dumps(rep, indent=1))

def grade2(qobj, answers):
    labels = list(answers); random.Random('x' + qobj['q']).shuffle(labels)
    anon = {f'X{i+1}': answers[l] for i, l in enumerate(labels)}
    txt = b.muse('grade_x', (
        'Grade each candidate answer against the reference. Question: ' + qobj['q'] + '\nReference answer: ' + qobj['answer'] +
        '\nSupporting quote from the original log: ' + qobj['quote'] + '\nThe candidates are in the attached JSON. For each candidate return one of: '
        '"correct" (matches the reference in substance), "partial" (partly right or missing the key specific), "wrong" (states something incorrect), '
        '"abstain" (says it does not know). Return ONLY a JSON object mapping candidate id to grade.'),
        [json.dumps(anon, ensure_ascii=False, indent=1)], model=GRADER2)
    res = b.parse_json(txt) or {}
    if not res: raise ValueError('grader2 returned nothing')
    return {l: str(res.get(f'X{i+1}', 'wrong')).lower() for i, l in enumerate(labels)}

if __name__ == '__main__':
    main()
