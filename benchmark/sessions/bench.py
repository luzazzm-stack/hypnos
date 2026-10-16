"""Memory architecture benchmark: A (flat BM25) vs B (rolling summary) vs D (v0.3-lite).
LLM = Muse Spark 1.3 via stock `opencode run`. Jev (TypeSafe) runs the judge/gate questions.
Everything is cached in cache/ so the run can be resumed."""
import json, os, re, sys, math, time, hashlib, subprocess, random, threading, urllib.request, glob
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, 'cache'); ATT = os.path.join(ROOT, 'work', 'att'); RUN = os.path.join(ROOT, 'run')
OUT = os.path.join(ROOT, 'out')
for d in (CACHE, ATT, RUN, OUT): os.makedirs(d, exist_ok=True)
MODEL = 'opencode/muse-spark-1.3-contributor-free'
SESS_DIR = os.path.expanduser('~/.claude/projects/<your-project-folder>')
SESSIONS = ['685d5a65', 'fdd6c4b8', '6e1b1f49', '5a358a78', '7b5b4155', '58e65007', '14aa6dd7', 'a3e06428',
            '621589e1', '50cf36ac', '91106c51', 'ee19018b', '5ffe793c', '9c237583', '92015bb0']
LOG_CAP = 90000; EPISODE_CHARS = 15000; CHUNK = 1500
WORKERS = int(os.environ.get('WORKERS', '4'))
OVERHEAD_IN = 10870   # opencode system prompt + tool schemas, measured with a 5-word prompt
lock = threading.Lock()
STATS = defaultdict(lambda: {'calls': 0, 'in': 0, 'out': 0, 'secs': 0.0, 'cached': 0})

def log(*a):
    with lock:
        print(time.strftime('%H:%M:%S'), *a, flush=True)

def h(*parts):
    return hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()[:24]

# ---------------- LLM via opencode ----------------
def muse(tag, prompt, attachments=(), model=None):
    model = model or MODEL
    key = h('muse', prompt, *attachments) if model == MODEL else h('muse', model, prompt, *attachments)
    cp = os.path.join(CACHE, key + '.json')
    if os.path.exists(cp):
        r = json.load(open(cp, encoding='utf-8'))
        with lock:
            s = STATS[tag]; s['calls'] += 1; s['cached'] += 1; s['in'] += r['in']; s['out'] += r['out']; s['secs'] += r['secs']
        return r['text']
    if os.environ.get('CACHE_ONLY'): raise LookupError('not cached: ' + tag)
    files = []
    for i, a in enumerate(attachments):
        p = os.path.join(ATT, f'{key}_{i}.txt')
        open(p, 'w', encoding='utf-8').write(a); files += ['-f', p]
    full = 'Do not use any tools. Answer directly from the attached file(s) and this message. ' + prompt
    env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(ROOT, 'cfg'))
    for attempt in range(8):
        t0 = time.time()
        try:
            p = subprocess.run(['opencode', 'run', '-m', model, '--format', 'json', *files, '--', full],
                               cwd=RUN, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
                               shell=False)
        except subprocess.TimeoutExpired:
            log(f'  muse timeout {attempt+1} [{tag}] (provider rate limit?) - waiting 60s')
            time.sleep(60); continue
        secs = time.time() - t0
        texts, tin, tout, err = [], 0, 0, None
        for line in p.stdout.splitlines():
            try: ev = json.loads(line)
            except Exception: continue
            if ev.get('type') == 'text': texts.append(ev['part'].get('text', ''))
            elif ev.get('type') == 'step_finish':
                tk = ev['part'].get('tokens', {}); tin += tk.get('input', 0); tout += tk.get('output', 0) + tk.get('reasoning', 0)
            elif ev.get('type') == 'error': err = json.dumps(ev.get('error'))[:300]
        text = '\n'.join(texts).strip()
        if text and not err:
            r = {'text': text, 'in': max(0, tin - OVERHEAD_IN), 'out': tout, 'secs': secs}
            json.dump(r, open(cp, 'w', encoding='utf-8'))
            with lock:
                s = STATS[tag]; s['calls'] += 1; s['in'] += r['in']; s['out'] += r['out']; s['secs'] += secs
            return text
        log(f'  muse retry {attempt+1} [{tag}] err={err or p.stderr[-200:]!r}')
        time.sleep(min(120, 20 * (attempt + 1)))
    raise RuntimeError('muse failed: ' + tag)

def parse_json(text):
    t = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M).strip()
    for opener, closer in (('[', ']'), ('{', '}')):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try: return json.loads(t[i:j+1])
            except Exception: pass
    return None

# ---------------- Jev ----------------
JEV_KEY = None
for line in open(os.path.expanduser('~/.typesafe/credentials'), encoding='utf-8'):
    if line.startswith('TYPESAFE_API_KEY='): JEV_KEY = line.split('=', 1)[1].strip()

def jev(tag, state, questions):
    key = h('jev', json.dumps(state, sort_keys=True), json.dumps(questions, sort_keys=True))
    cp = os.path.join(CACHE, key + '.json')
    if os.path.exists(cp):
        r = json.load(open(cp, encoding='utf-8'))
    else:
        body = json.dumps({'state': state, 'model': 'jev-latest', 'questions': questions}).encode('utf-8')
        r = None
        for attempt in range(6):
            t0 = time.time()
            try:
                req = urllib.request.Request('https://api.typesafe.ai/v1/systemone', data=body, method='POST',
                                             headers={'Authorization': 'Bearer ' + JEV_KEY, 'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                r = {'answers': data['answers'], 'in': data.get('usage', {}).get('input_tokens', 0),
                     'out': data.get('usage', {}).get('output_tokens', 0), 'secs': time.time() - t0, 'model': data.get('model')}
                json.dump(r, open(cp, 'w', encoding='utf-8'))
                break
            except Exception as e:
                log(f'  jev retry {attempt+1} [{tag}] {str(e)[:200]}')
                time.sleep(3 * (attempt + 1))
        if r is None: return None
    with lock:
        s = STATS['jev:' + tag]; s['calls'] += 1; s['in'] += r['in']; s['out'] += r['out']; s['secs'] += r['secs']
    return r['answers']

# ---------------- session parsing ----------------
def strip_noise(t):
    t = re.sub(r'<system-reminder>.*?</system-reminder>', '', t, flags=re.S)
    return re.sub(r'\n{3,}', '\n\n', t).strip()

def load_session(prefix):
    path = glob.glob(os.path.join(SESS_DIR, prefix + '*.jsonl'))[0]
    rows, first_ts = [], None
    def render(tool_cap, use_cap):
        out = []
        for line in open(path, encoding='utf-8', errors='replace'):
            try: ev = json.loads(line)
            except Exception: continue
            if ev.get('type') not in ('user', 'assistant') or ev.get('isSidechain') or ev.get('isMeta'): continue
            msg = ev.get('message') or {}; content = msg.get('content'); role = ev['type']
            blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else (content or [])
            for b in blocks:
                if not isinstance(b, dict): continue
                if b.get('type') == 'text':
                    t = strip_noise(b.get('text', ''))
                    if t: out.append(f'[{role}] {t[:1500]}')
                elif b.get('type') == 'tool_use':
                    out.append(f'[tool_call {b.get("name")}] {json.dumps(b.get("input"), ensure_ascii=False)[:use_cap]}')
                elif b.get('type') == 'tool_result':
                    c = b.get('content'); c = c if isinstance(c, str) else ' '.join(x.get('text', '') for x in (c or []) if isinstance(x, dict))
                    c = strip_noise(c)
                    if c: out.append(f'[tool_result] {c[:tool_cap]}')
        return out
    for line in open(path, encoding='utf-8', errors='replace'):
        m = re.search(r'"timestamp":"([^"]+)"', line)
        if m: first_ts = m.group(1); break
    rows = render(500, 300)
    if sum(len(r) + 1 for r in rows) > LOG_CAP: rows = render(200, 150)
    text = '\n'.join(rows)
    if len(text) > LOG_CAP:
        text = text[:LOG_CAP // 2] + '\n[... middle of session omitted ...]\n' + text[-LOG_CAP // 2:]
    return {'id': prefix, 'ts': first_ts or '', 'text': text}

def windows(text, size):
    out, cur = [], ''
    for line in text.split('\n'):
        if len(cur) + len(line) > size and cur: out.append(cur); cur = ''
        cur += line + '\n'
    if cur.strip(): out.append(cur)
    return out

def norm(s): return re.sub(r'\s+', ' ', s).strip().lower()

# ---------------- BM25 ----------------
TOK = re.compile(r'[a-z0-9_./\\-]+')
def toks(s): return [w for w in TOK.findall(s.lower()) if len(w) > 1]
class BM25:
    def __init__(self, docs):
        self.docs = docs; self.tf = [Counter(toks(d)) for d in docs]; self.len = [sum(t.values()) for t in self.tf]
        self.avg = sum(self.len) / max(1, len(self.len)); df = Counter()
        for t in self.tf: df.update(t.keys())
        n = len(docs); self.idf = {w: math.log(1 + (n - c + .5) / (c + .5)) for w, c in df.items()}
    def top(self, q, k):
        qs = toks(q); sc = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for w in qs:
                if w in tf: f = tf[w]; s += self.idf[w] * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * self.len[i] / self.avg))
            sc.append((s, i))
        return [i for s, i in sorted(sc, reverse=True)[:k] if s > 0]

def est(s): return len(s) // 4
def pmap(fn, items, workers=WORKERS):
    with ThreadPoolExecutor(workers) as ex: return list(ex.map(fn, items))

# ---------------- stage 1: data + questions ----------------
def build_questions(sessions):
    def gen(s):
        txt = muse('questions', (
            'The attached file is the log of one past work session between a user and an AI coding assistant. '
            'Write 5 test questions about specific, concrete things in this session that a memory system should be able to recall later: '
            'exact values, paths, commands, settings, names, decisions and their reasons, outcomes, fixes. '
            'Each question must make sense on its own, months later, without seeing the log: include enough context about what was being worked on, '
            'but not the answer. Avoid trivia about the assistant\'s wording. '
            'Return ONLY a JSON array of objects with keys: "q" (the question), "answer" (short correct answer), '
            '"quote" (a short passage, under 200 characters, copied EXACTLY character-for-character from the log, that proves the answer).'),
            [s['text']])
        arr = parse_json(txt) or []
        good = []
        for x in arr:
            if not isinstance(x, dict) or not all(k in x for k in ('q', 'answer', 'quote')): continue
            if norm(x['quote'])[:150] in norm(s['text']): good.append({**x, 'session': s['id']})
        return good[:4]
    qs = []
    for s, g in zip(sessions, pmap(gen, sessions)): qs += g; log(f'questions {s["id"]}: {len(g)} valid')
    return qs

# ---------------- condition A ----------------
def build_A(sessions):
    chunks = []
    for s in sessions:
        t = s['text']
        for i in range(0, len(t), CHUNK - 200): chunks.append(f'(session {s["id"]}) ' + t[i:i + CHUNK])
    return chunks, BM25(chunks)

# ---------------- condition B ----------------
def build_B(sessions):
    summary = 'No memories yet.'
    for s in sessions:
        summary = muse('B_sleep', (
            'You maintain a single rolling memory summary for an AI assistant that works with this user across many sessions. '
            'Attachment 1 is the current memory summary. Attachment 2 is the log of a new session. '
            'Rewrite the memory summary so it keeps the most useful older information and adds the important new information '
            '(specific values, paths, commands, decisions, outcomes, fixes). Hard limit: 1200 words. Return only the new summary.'),
            [summary, s['text']])
        log(f'B summary after {s["id"]}: {est(summary)} tokens')
    return summary

# ---------------- condition D (v0.3-lite) ----------------
KINDS = 'event, decision, outcome, lesson, convention, command, gotcha, failed_approach'
def sleep_D(sessions):
    episodes = []
    for s in sessions:
        for j, w in enumerate(windows(s['text'], EPISODE_CHARS)): episodes.append({'id': f'ep_{s["id"]}_{j:02d}', 'session': s['id'], 'text': w})
    log(f'D: {len(episodes)} episodes')

    def extract(ep):
        txt = muse('D_extract', (
            'The attached file is one part of a work session log between a user and an AI assistant. '
            'List the facts worth remembering for future sessions: specific values, file paths, commands, settings, decisions with reasons, outcomes, '
            f'fixes, gotchas and failed approaches. Up to 12 facts. Each fact: "claim" (one self-contained sentence with enough context), '
            f'"kind" (one of: {KINDS}), "quote" (a short passage under 200 characters copied EXACTLY from the log that supports the claim). '
            'Return ONLY a JSON array.'), [ep['text']])
        arr = parse_json(txt) or []
        facts = []
        for k, x in enumerate(arr):
            if isinstance(x, dict) and x.get('claim'):
                facts.append({'id': f'{ep["id"]}#f{k+1}', 'claim': x['claim'], 'kind': x.get('kind', 'event'), 'quote': x.get('quote', '')})
        return facts
    for ep, facts in zip(episodes, pmap(extract, episodes)): ep['facts'] = facts
    log(f'D: extracted {sum(len(e["facts"]) for e in episodes)} claims')

    def verify(ep):
        nt = ep['text']; low = nt.lower(); items = []
        for f in ep['facts']:
            q = f['quote'].strip(); pos = low.find(q.lower()[:120]) if q else -1
            if pos == -1:
                f['quote_found'] = False; f['muse_ok'] = False; f['jev_p'] = None; continue
            f['quote_found'] = True; f['span'] = nt[max(0, pos - 500): pos + len(q) + 500]; items.append(f)
        if not items: return
        txt = muse('D_verify', (
            'Each item in the attached JSON has a "claim" and an "evidence" span copied from a work log. For each item, judge ONLY from its own evidence span '
            'whether the evidence directly supports the claim (true) or not (false). Return ONLY a JSON object mapping item id to true or false.'),
            [json.dumps([{'id': f['id'], 'claim': f['claim'], 'evidence': f['span']} for f in items], ensure_ascii=False, indent=1)])
        res = parse_json(txt) or {}
        for f in items: f['muse_ok'] = bool(res.get(f['id'], False))
        state = {'items': {f'i{n}': {'claim': f['claim'], 'evidence': f['span']} for n, f in enumerate(items)}}
        qs = {f'v{n}': {'type': 'noul',
                        'instructions': f'Does the evidence text in `items.i{n}.evidence` directly support the claim in `items.i{n}.claim`? Judge only from that evidence.',
                        'criteria': {'true': 'The evidence states or clearly shows what the claim says.',
                                     'false': 'The evidence does not show it, contradicts it, or the claim adds details not in the evidence.'}}
              for n, f in enumerate(items)}
        ans = jev('verify', state, qs) or {}
        for n, f in enumerate(items): f['jev_p'] = ans.get(f'v{n}', {}).get('noul')
    pmap(verify, episodes)
    facts = [f for e in episodes for f in e['facts']]
    kept = [f for f in facts if f.get('muse_ok')]
    log(f'D: {len(kept)}/{len(facts)} claims verified (Muse decides; Jev shadow)')

    # topic assignment, sequential (the list grows)
    topics = {}  # name -> {'desc', 'episodes': []}
    agree = []
    for ep in episodes:
        ef = [f for f in ep['facts'] if f.get('muse_ok')]
        if not ef: continue
        listing = '\n'.join(f'- {n}: {t["desc"]}' for n, t in topics.items()) or '(none yet)'
        facts_txt = '\n'.join('- ' + f['claim'] for f in ef)
        txt = muse('D_topic', (
            'Existing memory topics:\n' + listing + '\n\nFacts from a new episode are in the attached file. '
            'Pick the ONE existing topic these facts mainly belong to, or create a new topic if none fits well. '
            'Topics should be about a subject (a project, device, tool, problem area), not a date. '
            'Return ONLY JSON: {"topic": "<existing name or new_short_snake_case_name>", "new": true|false, "desc": "<one-line description if new>"}'),
            [facts_txt])
        r = parse_json(txt) or {}
        name = re.sub(r'[^a-z0-9_]+', '_', str(r.get('topic', 'misc')).lower()).strip('_')[:40] or 'misc'
        is_new = name not in topics
        if topics:
            crit = {n: t['desc'] for n, t in topics.items()}; crit['new_topic'] = 'None of the existing topics fits these facts well.'
            ans = jev('topic', {'facts': [f['claim'] for f in ef]},
                      {'t': {'type': 'choice', 'instructions': 'Which existing memory topic do the facts in `facts` mainly belong to? Pick new_topic if none fits well.',
                             'criteria': crit}}) or {}
            jc = ans.get('t', {}).get('choice')
            if jc: agree.append((jc == ('new_topic' if is_new else name)))
        if is_new: topics[name] = {'desc': str(r.get('desc', name))[:160], 'episodes': []}
        topics[name]['episodes'].append(ep['id']); ep['topic'] = name
    log(f'D: {len(topics)} topics; Jev/Muse topic agreement {sum(agree)}/{len(agree)}')

    # topic ladder, each size written independently from the facts
    SIZES = {'small': (60, 'one or two sentences'), 'medium': (300, 'a short paragraph or a few bullets'), 'big': (1500, 'a detailed structured summary')}
    jobs = []
    for name, t in topics.items():
        tf = [f for e in episodes if e.get('topic') == name for f in e['facts'] if f.get('muse_ok')]
        t['facts'] = tf
        for size in SIZES: jobs.append((name, size, tf))
    def write(job):
        name, size, tf = job; tokens, shape = SIZES[size]
        return muse('D_topics', (
            f'Write the {size.upper()} memory summary for the topic "{name}" from the verified facts in the attached file. '
            f'Size: about {tokens} tokens, {shape}. Keep the most useful specifics (values, paths, commands, decisions, outcomes). '
            'Use only these facts. Return only the summary text.'),
            ['\n'.join(f'[{f["id"]}] ({f["kind"]}) {f["claim"]}' for f in tf)])
    for (name, size, _), txt in zip(jobs, pmap(write, jobs)): topics[name][size] = txt
    log('D: topic ladders written')
    return {'episodes': episodes, 'facts': kept, 'all_facts': facts, 'topics': topics, 'topic_agree': agree}

GATE_Q = {'enough': {'type': 'noul',
                     'instructions': 'Do the memories in `memories` contain the information needed to answer the question in `question` correctly and specifically?',
                     'criteria': {'true': 'The specific answer can be read from the memories.',
                                  'false': 'The memories are related at best; the specific answer is missing or unclear.'}}}

def recall_D(D, qobj, chunksA, bmA):
    topics = D['topics']; names = list(topics)
    bmT = BM25([n.replace('_', ' ') + ' ' + topics[n]['desc'] + ' ' + topics[n]['small'] for n in names])
    tops = [names[i] for i in bmT.top(qobj['q'], 3)] or names[:3]
    bmF = BM25([f['claim'] for f in D['facts']])
    # R7 evidence rule: summaries paraphrase, so the facts they were written from travel with them (claim + exact quote)
    tf = [f for n in tops for f in topics[n].get('facts', [])]
    ev = '\n'.join(f'[{tf[i]["id"]}] {tf[i]["claim"]} | evidence: "{tf[i]["quote"]}"' for i in BM25([f['claim'] + ' ' + f['quote'] for f in tf]).top(qobj['q'], 8)) if tf else ''
    evb = ('\n\n[evidence facts]\n' + ev) if ev else ''
    levels = [
        ('small', lambda: '\n\n'.join(f'[topic {n} / small] {topics[n]["small"]}' for n in tops) + evb),
        ('medium', lambda: '\n\n'.join(f'[topic {n} / medium] {topics[n]["medium"]}' for n in tops) + evb),
        ('big', lambda: '\n\n'.join(f'[topic {n} / big] {topics[n]["big"]}' for n in tops) + evb),
        ('facts', lambda: '\n'.join(f'[{D["facts"][i]["id"]}] {D["facts"][i]["claim"]} | evidence: "{D["facts"][i]["quote"]}"' for i in bmF.top(qobj['q'], 12))),
        ('raw', lambda: '\n\n'.join(chunksA[i] for i in bmA.top(qobj['q'], 3))),
    ]
    summ, extra, trace = '', [], []
    for lvl, fn in levels:
        if lvl in ('small', 'medium', 'big'): summ = fn()          # zoom in: the bigger size replaces the smaller
        else: extra.append(fn())                                  # facts, then raw log, are added on top of big
        ctx = '\n\n'.join([summ] + extra)
        ans = jev('gate', {'question': qobj['q'], 'memories': ctx}, GATE_Q) or {}
        p = ans.get('enough', {}).get('noul')
        mt = muse('D_gate_shadow', (
            'Question: ' + qobj['q'] + '\nDo the memories in the attached file contain the information needed to answer this question correctly and specifically? '
            'Reply with only YES or NO.'), [ctx])
        m_yes = 'YES' in mt.upper()[:20]
        trace.append({'level': lvl, 'jev_p': p, 'muse_yes': m_yes, 'ctx_tokens': est(ctx)})
        decided = (p >= 0.85) if p is not None else m_yes
        if decided: break
    return ctx, trace

def answer(tag, q, ctx):
    return muse(tag, (
        'You are an AI assistant answering from your memory of past sessions with this user. Your memory is in the attached file. '
        'Question: ' + q + '\nAnswer briefly and specifically using only the memory. If the memory does not contain the answer, reply exactly: I don\'t know.'),
        [ctx])

def grade(qobj, answers):
    labels = list(answers); random.Random(qobj['q']).shuffle(labels)
    anon = {f'X{i+1}': answers[l] for i, l in enumerate(labels)}
    txt = muse('grade', (
        'Grade each candidate answer against the reference. Question: ' + qobj['q'] + '\nReference answer: ' + qobj['answer'] +
        '\nSupporting quote from the original log: ' + qobj['quote'] + '\nThe candidates are in the attached JSON. For each candidate return one of: '
        '"correct" (matches the reference in substance), "partial" (partly right or missing the key specific), "wrong" (states something incorrect), '
        '"abstain" (says it does not know). Return ONLY a JSON object mapping candidate id to grade.'),
        [json.dumps(anon, ensure_ascii=False, indent=1)])
    res = parse_json(txt) or {}
    return {l: str(res.get(f'X{i+1}', 'wrong')).lower() for i, l in enumerate(labels)}

def main():
    global OUT
    t0 = time.time()
    names = SESSIONS
    if os.environ.get('SMOKE'):
        names = SESSIONS[-2:]; OUT = os.path.join(ROOT, 'out_smoke'); os.makedirs(OUT, exist_ok=True)
    sessions = sorted((load_session(p) for p in names), key=lambda s: s['ts'])
    log('sessions:', ' '.join(f'{s["id"]}({len(s["text"])//1000}k)' for s in sessions))
    qs = build_questions(sessions); log(f'{len(qs)} questions'); json.dump(qs, open(os.path.join(OUT, 'questions.json'), 'w', encoding='utf-8'), indent=1)
    chunksA, bmA = build_A(sessions)
    B = build_B(sessions)
    D = sleep_D(sessions)

    def run_q(q):
        ctxA = '\n\n'.join(chunksA[i] for i in bmA.top(q['q'], 6))
        aA = answer('A_answer', q['q'], ctxA)
        aB = answer('B_answer', q['q'], B)
        ctxD, trace = recall_D(D, q, chunksA, bmA)
        aD = answer('D_answer', q['q'], ctxD)
        g = grade(q, {'A': aA, 'B': aB, 'D': aD})
        log(f'Q done [{g["A"][:4]} {g["B"][:4]} {g["D"][:4]}] depth={trace[-1]["level"]}')
        return {**q, 'ans': {'A': aA, 'B': aB, 'D': aD}, 'grade': g,
                'ctx_tokens': {'A': est(ctxA), 'B': est(B), 'D': est(ctxD)}, 'trace': trace}
    def safe(q):
        try: return run_q(q)
        except LookupError: return None
    results = [r for r in pmap(safe, qs) if r]
    log(f'{len(results)}/{len(qs)} questions complete')

    # ---------- report ----------
    rep = {'n_questions': len(results), 'minutes': round((time.time() - t0) / 60, 1)}
    for c in 'ABD':
        gs = [r['grade'][c] for r in results]
        rep[c] = {'accuracy': round((gs.count('correct') + .5 * gs.count('partial')) / len(gs), 3),
                  'correct': gs.count('correct'), 'partial': gs.count('partial'), 'wrong': gs.count('wrong'), 'abstain': gs.count('abstain'),
                  'false_memory_rate': round(gs.count('wrong') / len(gs), 3),
                  'avg_context_tokens': round(sum(r['ctx_tokens'][c] for r in results) / len(results))}
    rep['D_depth'] = dict(Counter(r['trace'][-1]['level'] for r in results))
    gate = [(t['jev_p'] >= 0.85, t['muse_yes']) for r in results for t in r['trace'] if t['jev_p'] is not None]
    rep['gate_agreement'] = f'{sum(a == b for a, b in gate)}/{len(gate)}'
    fa = [f for f in D['all_facts'] if f.get('quote_found') and f.get('jev_p') is not None]
    rep['verify'] = {'claims': len(D['all_facts']), 'quote_not_found': sum(1 for f in D['all_facts'] if not f.get('quote_found')),
                     'muse_kept': len(D['facts']), 'jev_kept@0.5': sum(1 for f in fa if f['jev_p'] >= .5),
                     'agreement@0.5': f'{sum((f["jev_p"] >= .5) == f["muse_ok"] for f in fa)}/{len(fa)}'}
    rep['topics'] = {'count': len(D['topics']), 'jev_muse_agreement': f'{sum(D["topic_agree"])}/{len(D["topic_agree"])}'}
    rep['wake_tokens'] = {'B_summary': est(B), 'D_all_topic_smalls': sum(est(t['small']) for t in D['topics'].values())}
    rep['stats'] = {k: {**v, 'secs': round(v['secs'], 1)} for k, v in sorted(STATS.items())}
    json.dump(rep, open(os.path.join(OUT, 'report.json'), 'w', encoding='utf-8'), indent=1)
    json.dump(results, open(os.path.join(OUT, 'results.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    json.dump({'topics': {n: {k: v for k, v in t.items() if k != 'facts'} for n, t in D['topics'].items()}, 'B_summary': B},
              open(os.path.join(OUT, 'memory.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    log('REPORT', json.dumps(rep, indent=1))

if __name__ == '__main__':
    main()
