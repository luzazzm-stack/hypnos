"""The memory engine (architecture v0.9): log, sleep, recall, wake, pin/correct/forget, review/cleanup, export."""
from __future__ import annotations
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import jev as J
from .config import Config, load_config, safe_space
from .llm import Writer, make_writer, parse_json
from .profiles import get_profile
from .filters import is_refusal
from .redact import redact
from .store import Store, now, STOP, entity_id
from .vectors import Vectors


def est(text: str) -> int:
    return len(text or '') // 4


ANCHOR_RE = re.compile(
    r'`([^`]{2,60})`|([A-Za-z]:\\[^\s"\'|,;)]+|~?/?[\w.\-]+(?:/[\w.\-]+)+)|((?<!\w)--[a-z][\w-]{2,})'
    r'|(\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b)'
    r'|(\b[\w-]+\.(?:ps1|py|json|md|ini|cfg|exe|bat|sh|html|js|ts|tsx|yaml|yml|toml|apk|log|txt|zip|sql|go|rs|java)\b)'
    r'|(\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b)|(\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b)|(\b[\w.-]*\d[\w.-]*\d[\w.-]*\b)')


def anchors_for(facts, cap=30):
    seen, out = set(), []
    for f in facts:
        for m in ANCHOR_RE.finditer((f['claim'] or '') + ' ' + (f['quote'] or '')):
            a = next(g for g in m.groups() if g).strip().strip('.,')
            if len(a) >= 3 and a.lower() not in seen:
                seen.add(a.lower()); out.append(a)
    return out[:cap]


def _words(text):
    return {w for w in re.findall(r'[a-z0-9_]{3,}', (text or '').lower()) if w not in STOP}


@dataclass
class Recall:
    context: str = ''
    exact: bool = False
    level: str = 'none'
    tokens: int = 0
    topics: list = field(default_factory=list)
    trace: list = field(default_factory=list)

    def as_dict(self):
        return self.__dict__


class Memory:
    def __init__(self, space: str = 'default', profile: str = 'generic', cfg: Config | None = None, writer: Writer | None = None):
        self.cfg = cfg or load_config()
        self.space = safe_space(space)
        self.profile = get_profile(profile)
        self.store = Store(self.cfg.db_path(self.space))
        self._writer = writer
        self.jev = J.Jev(self.cfg, self.store)
        self.vec = Vectors(self.cfg, self.store)

    @property
    def writer(self) -> Writer:
        if self._writer is None:
            self._writer = make_writer(self.cfg)
        return self._writer

    # ------------------------------------------------------------------ awake
    def log(self, content: str, role: str = 'user', session: str = 'default', meta: dict | None = None, fast: bool | None = None) -> int:
        """R1: redact locally, then append. Never edited afterwards.
        Real-time path: a message that states something worth remembering (a preference, decision, personal fact,
        correction) becomes verified facts right away instead of waiting for sleep. fast=None decides automatically."""
        clean, kinds = redact(str(content))
        lid = self.store.append(session, role, clean, meta, kinds)
        if fast or (fast is None and self._worth_now(clean, role)):
            if self.fast_async:
                self._pool().submit(self._fast_extract, lid)
            else:
                self._fast_extract(lid)
        return lid

    fast_async = False                     # the service sets this True so logging never waits on the fast path
    _executor = None

    def _pool(self):
        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._executor = ThreadPoolExecutor(1)
        return self._executor

    _WORTH = re.compile(r"(?i)\b(i (?:prefer|like|love|hate|dislike|use|switched|moved|decided|want|always|never|am|'m|work|live)|"
                        r"my (?:name|birthday|wife|husband|partner|son|daughter|job|email|favorite|favourite)|remember (?:that|this)|"
                        r"from now on|call me|we decided|don't ever|do not ever|going forward|actually,? it'?s)\b")

    def _worth_now(self, text, role):
        if role not in ('user', 'fact') or len(text) > 3000 or len(text) < 12:
            return False
        p = self.jev.noul({'message': text[:2000]}, 'Does the message in `message` state something worth remembering long-term '
                          '(a preference, a decision, a fact about the user or their work, a correction, or a plan)?',
                          'It states something durable worth remembering.', 'Small talk, a question, or nothing durable.')
        return (p >= 0.6) if p is not None else bool(self._WORTH.search(text))

    def _fast_extract(self, lid):
        """Real-time fact path: extract from one message (+ two before it for context), verify by quote, store now.
        Summaries are refreshed at the next sleep; the same message is de-duplicated when sleep sees it again."""
        try:
            row = self.store.q('SELECT * FROM log WHERE id=?', (lid,))[0]
            prev = self.store.q('SELECT * FROM log WHERE session=? AND id<? ORDER BY id DESC LIMIT 2', (row['session'], lid))
            win = [(r, f'[#{r["id"]} {r["role"]}] {r["content"][:2000]}') for r in list(reversed(prev)) + [row]]
            self._consolidate_episode(row['session'], win, ep_id=f'fast_{lid}', only_row=lid, allow_writer_topic=False)
        except Exception as ex:
            print(f'[hypnos] fast path failed: {ex}', flush=True)

    # ------------------------------------------------------------------ recall (v0.7)
    def _rrf(self, *rankings, k=60):
        score = defaultdict(float)
        for ranking in rankings:
            for r, key in enumerate(ranking):
                score[key] += 1 / (k + r)
        return [key for key, _ in sorted(score.items(), key=lambda x: -x[1])]

    def _query_terms(self, query):
        """Optional query expansion (v0.7 #5): related search words from the writer, e.g. 'musical artists' -> concert, band."""
        if not self.cfg.expand_query:
            return query
        try:
            extra = self.writer.complete('List up to 8 extra search keywords (synonyms and closely related words, no explanations) '
                                         'that would help find the answer to this question in old chat messages. Reply with the words '
                                         'separated by spaces only. Question: ' + query)
            return query + ' ' + re.sub(r'[^\w\s-]', ' ', extra)[:200]
        except Exception:
            return query

    def _query_entities(self, query):
        """Entities (people, places, things) named in the question, matched against the entity index."""
        ql = f' {query.lower()} '
        return [e['id'] for e in self.store.q('SELECT id, name FROM entities')
                if len(e['name'] or '') >= 3 and re.search(r'(?<!\w)' + re.escape(e['name'].lower()) + r'(?!\w)', ql)]

    # ------------------------------------------------------------------ recall (v0.8)
    def in_context(self, session):
        """v0.9: log rows of this session that the agent still sees (logged after its last compaction). Recalling them
        would only echo the conversation back, so recall skips them."""
        if not session:
            return set()
        mark = int(self.store.note('compact:' + session) or 0)
        return {r['id'] for r in self.store.q('SELECT id FROM log WHERE session=? AND id>?', (session, mark))}

    def has_memory_beyond(self, session) -> bool:
        """Is there anything in memory besides the conversation the agent is in? (no Jev calls when not)"""
        if self.store.q("SELECT 1 FROM facts WHERE status='active' LIMIT 1"):
            return True
        n = self.store.q('SELECT COUNT(*) FROM log WHERE tombstone IS NULL')[0][0]
        return n > len(self.in_context(session))

    def mark_compacted(self, session):
        """The agent's context was compacted: older rows of this session are no longer in view, so recall may use them."""
        top = self.store.q('SELECT MAX(id) FROM log WHERE session=?', (session,))[0][0]
        if top:
            self.store.note('compact:' + session, str(top))

    @staticmethod
    def _evidence_row(f):
        ev = json.loads(f['evidence'] or '[]')
        try:
            return int(ev[0][4:].split('-')[0]) if ev and ev[0].startswith('log:') else None
        except ValueError:
            return None

    def _gather(self, query, n_facts=25, n_logs=15, n_ent=30, skip=frozenset()):
        """Candidates from every layer, cheaply (code only): hybrid-ranked facts and raw messages, all facts about the
        people/things the question names (meaning-ranked), and the best-matching topic with its three summaries."""
        s = self.store
        terms = self._query_terms(query)
        rows = {f['id']: f for f in s.search_facts(terms, 30)}
        fids = self._rrf(list(rows), self.vec.search('fact', query, 30))
        miss = [i for i in fids if i not in rows]
        if miss:
            rows.update({r['id']: r for r in s.q(f"SELECT * FROM facts WHERE status='active' AND id IN ({','.join('?' * len(miss))})", miss)})
        facts = [rows[i] for i in fids if i in rows and self._evidence_row(rows[i]) not in skip][:n_facts]
        lrows = {r['id']: r for r in s.search_log(terms, 30)}
        lids = self._rrf(list(lrows), [int(x) for x in self.vec.search('log', query, 30)])
        miss = [i for i in lids if i not in lrows]
        if miss:
            lrows.update({r['id']: r for r in s.q(f"SELECT * FROM log WHERE tombstone IS NULL AND id IN ({','.join('?' * len(miss))})", miss)})
        logs = [lrows[i] for i in lids if i in lrows and i not in skip][:n_logs]
        ents, seen = [], {f['id'] for f in facts}
        qents = self._query_entities(query)
        if qents:
            cand = {f['id']: f for f in s.facts_with_entities(qents, exclude=list(seen), k=400)}
            ents = [cand[i] for i in self.vec.rank('fact', query, list(cand)) if self._evidence_row(cand[i]) not in skip][:n_ent]
        ep_topic = {r['id']: r['topic'] for r in s.q('SELECT id, topic FROM episodes')}
        votes = defaultdict(float)
        for r, row in enumerate(s.search_topics(terms, 10)):
            votes[row['tid']] += 1 / (60 + r)
        for r, f in enumerate(facts[:12]):
            votes[ep_topic.get(f['episode'], '')] += 1 / (60 + r)
        votes.pop('', None); votes.pop(None, None)
        top = max(votes, key=votes.get) if votes else None
        topic = s.q('SELECT * FROM topics WHERE id=?', (top,))[0] if top else None
        return facts, logs, ents, topic

    def recall(self, query: str, budget: int = 6000, session: str | None = None, live: set | frozenset = frozenset()) -> Recall:
        """v0.8: Jev-routed recall. Code gathers candidates from every layer; Jev (free, parallel) decides the question
        type, which summary level already answers it, and which individual facts and messages are part of the answer.
        The context is the smallest set that answers: a summary for overview questions (small, then medium, then big),
        only the Jev-kept evidence otherwise, every kept item for list questions. A final Jev check expands if needed."""
        from concurrent.futures import ThreadPoolExecutor
        s, cfg = self.store, self.cfg
        if not s.q('SELECT 1 FROM facts LIMIT 1') and not s.q('SELECT 1 FROM log LIMIT 1'):
            return Recall()
        skip = self.in_context(session)
        if not getattr(self.jev, 'routing', self.jev.enabled) or not self.jev.enabled:
            return self.recall_v07(query, budget, skip, set(live) - {session})   # no Jev: the v0.7 evidence-first recall
        facts, logs, ents, topic = self._gather(query, skip=skip)
        if skip and not (facts or logs or ents or topic):                  # only this conversation so far: nothing to add
            return Recall()

        # v0.9: where each item came from. Items from another session that is running right now are marked, so an agent
        # never mistakes another session's request or plan for its own task.
        ep_sess = {r['id']: r['session'] for r in s.q('SELECT id, session FROM episodes')} if live else {}

        def origin(sess):
            return ' [from another session running now: not your task]' if sess and sess in live and sess != session else ''

        def fact_text(f):
            return (f'({(f["valid_from"] or "")[:10]}) {f["claim"]}  evidence: "{(f["quote"] or "")[:160]}"'
                    + origin(ep_sess.get(f['episode'])))

        def msg_text(r):
            return f'[{(r["ts"] or "")[:10]} log:{r["id"]}{origin(r["session"])}] {r["content"][:700]}'

        items = [('f', f['id'], fact_text(f)) for f in facts + ents] + [('m', r['id'], msg_text(r)) for r in logs]
        levels = [lv for lv in ('small', 'medium', 'big') if topic and topic[lv]]

        def route():
            a = self.jev.many({'question': query}, {'intent': ('choice', J.INTENT[0], J.INTENT[1])}, role='route')
            return (a or {}).get('intent', {}).get('choice') or 'single_fact'

        def summary_ok(lv):
            return lv, self.jev.noul({'question': query, 'summary': topic[lv][:6000]}, *J.SUMMARY_ENOUGH)

        def gate(it):
            a = self.jev.many({'question': query, 'memory': it[2]}, {'rel': ('noul',) + (J.ITEM_RELEVANT[0], {'true': J.ITEM_RELEVANT[1], 'false': J.ITEM_RELEVANT[2]}),
                                                                   'use': ('noul',) + (J.ITEM_USABLE[0], {'true': J.ITEM_USABLE[1], 'false': J.ITEM_USABLE[2]})})
            a = a or {}
            return it, (a.get('rel') or {}).get('noul'), (a.get('use') or {}).get('noul')

        with ThreadPoolExecutor(cfg.jev_parallel) as ex:                   # all Jev work for this question at once
            f_intent = ex.submit(route)
            f_sum = [ex.submit(summary_ok, lv) for lv in levels]
            f_items = [ex.submit(gate, it) for it in items]
            intent = f_intent.result()
            sums = dict(f.result() for f in f_sum)
            gated = [f.result() for f in f_items]

        res = Recall(exact=intent == 'exact_value', topics=[topic['name']] if topic else [])
        res.trace.append({'intent': intent, 'summaries': sums})
        # overview: the smallest summary level that answers is the whole context (the small -> medium -> big ladder)
        if intent == 'overview':
            for lv in levels:
                if (sums.get(lv) or 0) >= cfg.summary_min_probability:
                    res.context, res.level = f'[topic {topic["name"]} / {lv}] {topic[lv]}', lv
                    res.tokens = est(res.context)
                    return res
        # evidence: keep what Jev says is part of the answer, most useful first; lists keep every useful item
        def usable(g):
            return g[2] if g[2] is not None else 0.0
        keep_min = cfg.item_min_probability
        ranked = sorted(gated, key=lambda g: -usable(g))
        cap = 40 if intent == 'list' else 12
        kept = [g for g in ranked if usable(g) >= keep_min][:cap]
        backup = [g for g in ranked if g not in kept and (g[1] or 0) >= 0.5]   # on topic, not clearly part of the answer

        def render(sel):
            fs = [g[0][2] for g in sel if g[0][0] == 'f']
            ms = sorted((g[0] for g in sel if g[0][0] == 'm'), key=lambda it: it[1])
            parts = []
            if fs:
                parts.append('[facts]\n' + '\n'.join('- ' + t for t in fs))
            if ms:
                parts.append('[original messages]\n' + '\n'.join(it[2] for it in ms))
            return '\n\n'.join(parts)

        ctx, level = render(kept), 'evidence'
        if not kept or intent in ('single_fact', 'when', 'exact_value', 'list'):
            p = self.jev.noul({'question': query, 'evidence': ctx}, *J.ANSWERABLE) if kept else 0.0
            if (p or 0) < cfg.gate_min_probability and backup:            # not answered yet: add on-topic items
                kept = kept + backup[:12]
                ctx, level = render(kept), 'evidence+'
            res.trace.append({'answerable': p})
        if intent != 'exact_value' and levels and (sums.get('small') or 0) >= 0.3:
            ctx = f'[topic {topic["name"]} / small] {topic["small"][:400]}\n\n' + ctx   # a line of orientation
        while est(ctx) > budget and '\n' in ctx:
            ctx = ctx[:ctx.rfind('\n')]
        res.context, res.level, res.tokens = ctx, level, est(ctx)
        for g in kept:
            if g[0][0] == 'f':
                s.x('UPDATE facts SET recalled = recalled + 1, last_used=? WHERE id=?', (now(), g[0][1]))
        return res

    def recall_v07(self, query: str, budget: int = 6000, skip=frozenset(), others=frozenset()) -> Recall:
        """v0.7: evidence first. Hybrid search (keyword + meaning, fused) over facts AND raw messages is the first step;
        summaries only orient. Named people/things pull in all their facts (list questions). The gate stops only when the
        evidence itself answers the question (R15 extended to every question)."""
        s, cfg = self.store, self.cfg
        if not s.q('SELECT 1 FROM facts LIMIT 1') and not s.q('SELECT 1 FROM log LIMIT 1'):
            return Recall()
        terms = self._query_terms(query)

        # 1. hybrid search: facts (BM25 + vectors) and raw messages (BM25 + vectors), each fused with RRF
        fact_rows = {f['id']: f for f in s.search_facts(terms, 30)}
        vf = self.vec.search('fact', query, 30)
        fids = self._rrf(list(fact_rows), vf)
        missing = [i for i in fids if i not in fact_rows]
        if missing:
            for row in s.q(f"SELECT * FROM facts WHERE status='active' AND id IN ({','.join('?' * len(missing))})", missing):
                fact_rows[row['id']] = row
        facts = [fact_rows[i] for i in fids if i in fact_rows and self._evidence_row(fact_rows[i]) not in skip]
        log_rows = {r['id']: r for r in s.search_log(terms, 30)}
        vl = [int(x) for x in self.vec.search('log', query, 30)]
        lids = self._rrf(list(log_rows), vl)
        missing = [i for i in lids if i not in log_rows]
        if missing:
            for row in s.q(f"SELECT * FROM log WHERE tombstone IS NULL AND id IN ({','.join('?' * len(missing))})", missing):
                log_rows[row['id']] = row
        logs = [log_rows[i] for i in lids if i in log_rows and i not in skip]

        # 2. entity gather: every fact about the people/things the question names, most relevant first
        qents = self._query_entities(query)
        rank = {fid: r for r, fid in enumerate(fids)}
        ent_facts = []
        if qents:
            seen = {f['id'] for f in facts[:12]}
            cand = {f['id']: f for f in s.facts_with_entities(qents, exclude=list(seen), k=400)}
            order = self.vec.rank('fact', query, list(cand))
            if order == list(cand):                                      # no vectors: keyword rank, then newest
                order.sort(key=lambda i: rank.get(i, 10 ** 6))
            ent_facts = [cand[i] for i in order if self._evidence_row(cand[i]) not in skip]

        # topic orientation: the topic most of the top facts belong to
        ep_topic = {r['id']: r['topic'] for r in s.q('SELECT id, topic FROM episodes')}
        votes = defaultdict(float)
        for r, f in enumerate(facts[:12]):
            votes[ep_topic.get(f['episode'], '')] += 1 / (60 + r)
        votes.pop('', None); votes.pop(None, None)
        top = max(votes, key=votes.get) if votes else None
        topic = s.q('SELECT * FROM topics WHERE id=?', (top,))[0] if top else None

        stale_ids = {f['id'] for f in facts[:12] + ent_facts[:12] if self.profile.stale(f)}
        ep_sess = {r['id']: r['session'] for r in s.q('SELECT id, session FROM episodes')} if others else {}
        not_yours = ' [from another session running now: not your task]'

        def fmt(f):
            flag = ' [STALE: re-check before relying on it]' if f['id'] in stale_ids else ''
            src = '' if f['source'] == 'observed' else f' ({f["source"]})'
            line = f'- ({(f["valid_from"] or "")[:10]}) {f["claim"]}{src}{flag}  evidence: "{(f["quote"] or "")[:160]}"'
            if ep_sess.get(f['episode']) in others:
                line += not_yours
            for old in s.q('SELECT claim, valid_from, valid_to FROM facts WHERE superseded_by=?', (f['id'],)):   # R6
                line += f'\n  replaced earlier fact: "{old["claim"]}" (true {(old["valid_from"] or "")[:10]} to {(old["valid_to"] or "")[:10]})'
            return line

        def msg(r):
            tag = not_yours if r['session'] in others else ''
            return f'[{(r["ts"] or "")[:10]} log:{r["id"]}{tag}] {r["content"][:700]}'

        def with_neighbours(rows, n):
            ids = []
            for r in rows[:n]:
                for i in (r['id'] - 1, r['id'], r['id'] + 1):
                    if i not in ids and i not in skip:
                        ids.append(i)
            got = {r['id']: r for r in s.q(f"SELECT * FROM log WHERE tombstone IS NULL AND id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            out, prev = [], None
            for i in sorted(got):
                if prev is not None and i != prev + 1:
                    out.append('...')
                out.append(msg(got[i])); prev = i
            return out

        def uniq(rows, taken):
            out = []
            for f in rows:
                key = re.sub(r'\W+', ' ', (f['claim'] or '').lower()).strip()
                if key not in taken:
                    taken.add(key); out.append(f)
            return out

        def build(n_facts, n_ent, n_logs, neighbours):
            """Evidence first; the small topic summary only orients. Trimming for the budget drops extra facts first,
            then messages, and never the best facts."""
            taken = set()
            best = uniq(facts, taken)[:n_facts]
            more = uniq(ent_facts, taken)[:n_ent]
            lines = (with_neighbours(logs, n_logs) if neighbours else [msg(r) for r in logs[:n_logs]]) if logs else []
            head = f'[topic {topic["name"]} / small] {topic["small"][:500]}' if topic and topic['small'] else ''

            def render(m, l):
                parts = [head] if head else []
                if best:
                    parts.append('[facts]\n' + '\n'.join(fmt(f) for f in best))
                if lines[:l]:
                    parts.append('[original messages]\n' + '\n'.join(lines[:l]))
                if more[:m]:
                    parts.append('[more facts about the people and things in the question]\n' + '\n'.join(fmt(f) for f in more[:m]))
                return '\n\n'.join(parts)
            m, l = len(more), len(lines)
            ctx = render(m, l)
            while est(ctx) > budget and (m or l > 4):
                if m:
                    m -= 1
                else:
                    l -= 1
                ctx = render(m, l)
            return ctx

        res = Recall(exact=True, topics=[topic['name']] if topic else [])
        steps = [('evidence', lambda: build(12, 6, 8, False)),
                 ('deep', lambda: build(15, 30, 14, True))]
        for level, make in steps:
            ctx = make()
            while est(ctx) > budget and '\n' in ctx:                     # last resort: trim from the end
                ctx = ctx[:ctx.rfind('\n')]
            if level == steps[-1][0]:
                res.context, res.level = ctx, level
                res.trace.append({'level': level, 'p': None, 'tokens': est(ctx)})
                break
            p = self.jev.noul({'question': query, 'evidence': ctx}, *J.ANSWERABLE)
            enough = (p >= cfg.gate_min_probability) if p is not None else self._covers(query, ctx, 0.8)
            res.trace.append({'level': level, 'p': p, 'tokens': est(ctx)})
            if enough:
                res.context, res.level = ctx, level
                break
        for f in facts[:15] + ent_facts[:25]:                              # bookkeeping only (R9)
            s.x('UPDATE facts SET recalled = recalled + 1, last_used=? WHERE id=?', (now(), f['id']))
        res.tokens = est(res.context)
        return res

    def index_log(self):
        """Meaning-search index over raw messages (v0.7). Runs at sleep; call once to backfill an older memory."""
        rows = self.store.q('SELECT id, content FROM log WHERE tombstone IS NULL')
        self.vec.index('log', [(str(r['id']), r['content']) for r in rows if (r['content'] or '').strip()])

    def recall_v06(self, query: str, budget: int = 6000) -> Recall:
        """v0.6: search every layer at once, answer from the smallest layer that is enough; exact values only from evidence (R15)."""
        s, cfg = self.store, self.cfg
        if not s.q('SELECT 1 FROM facts LIMIT 1') and not s.q('SELECT 1 FROM log LIMIT 1'):
            return Recall()
        # G8: does the question ask for an exact value?
        p = self.jev.noul({'question': query}, *J.EXACT, role='gate')
        exact = (p >= 0.5) if p is not None else not re.match(r'(?i)\s*(why|how come|explain|summari[sz]e|describe|what do you think)', query)

        # 1. search every layer, fuse by topic
        votes = defaultdict(float)
        for r, row in enumerate(s.search_topics(query, 15)):
            votes[row['tid']] += 1 / (60 + r)
        facts = list(s.search_facts(query, 20))
        vfacts = self.vec.search('fact', query, 20)
        if vfacts:                                   # fuse vector hits into the fact ranking
            byid = {f['id']: f for f in facts}
            score = defaultdict(float)
            for r, f in enumerate(facts): score[f['id']] += 1 / (60 + r)
            for r, fid in enumerate(vfacts): score[fid] += 1 / (60 + r)
            extra = [x for x in vfacts if x not in byid]
            if extra:
                marks = ','.join('?' * len(extra))
                for row in s.q("SELECT * FROM facts WHERE status='active' AND id IN (" + marks + ")", extra):
                    byid[row['id']] = row
            facts = [byid[i] for i, _ in sorted(score.items(), key=lambda x: -x[1]) if i in byid]
        ep_topic = {r['id']: r['topic'] for r in s.q('SELECT id, topic FROM episodes')}
        for r, f in enumerate(facts):
            votes[ep_topic.get(f['episode'], '')] += 1 / (60 + r)
        logs = s.search_log(query, 10)
        ranges = [(e['start_id'], e['end_id'], e['topic']) for e in s.q('SELECT start_id, end_id, topic FROM episodes')]
        for r, row in enumerate(logs):
            for a, b, t in ranges:
                if a <= row['id'] <= b:
                    votes[t] += 1 / (60 + r)
        votes.pop('', None); votes.pop(None, None)
        tops = [t for t, _ in sorted(votes.items(), key=lambda x: -x[1])[:2]]
        topics = {t['id']: t for t in s.q(f'SELECT * FROM topics WHERE id IN ({",".join("?" * len(tops))})', tops)} if tops else {}

        facts = facts[:8]
        # multi-hop: one hop through shared entities (people, files, devices ...) of the best facts
        ents = set().union(*s.entities_of([f['id'] for f in facts[:5]]).values()) if facts else set()
        related = s.facts_with_entities(list(ents)[:12], exclude=[f['id'] for f in facts], k=5) if ents else []
        stale_ids = {f['id'] for f in facts + related if self.profile.stale(f)}
        for f in facts + related:                                        # bookkeeping only (R9)
            s.x('UPDATE facts SET recalled = recalled + 1, last_used=? WHERE id=?', (now(), f['id']))

        def fmt(f):
            flag = ' [STALE: re-check before relying on it]' if f['id'] in stale_ids else ''
            src = '' if f['source'] == 'observed' else f' ({f["source"]})'
            line = f'- ({(f["valid_from"] or "")[:10]}) {f["claim"]}{src}{flag}\n  evidence: "{(f["quote"] or "")[:220]}" [{", ".join(json.loads(f["evidence"] or "[]"))}]'
            for old in s.q('SELECT claim, valid_from, valid_to FROM facts WHERE superseded_by=?', (f['id'],)):   # R6
                line += f'\n  replaced earlier fact: "{old["claim"]}" (true {(old["valid_from"] or "")[:10]} to {(old["valid_to"] or "")[:10]})'
            return line

        def fact_block():
            out = '[facts]\n' + '\n'.join(fmt(f) for f in facts) if facts else ''
            if related:
                out += '\n\n[related facts, linked through the same people or things]\n' + '\n'.join(fmt(f) for f in related)
            return out

        topic_ranges = [(a, b) for a, b, t in ranges if t in tops]

        def topic_log(limit):
            rows, used, out = s.search_log(query, 30, topic_ranges), 0, []
            for row in rows:
                seg = f'[log:{row["id"]} {row["role"]}] {row["content"][:1500]}'
                if used + est(seg) > limit:
                    break
                out.append(seg); used += est(seg)
            return '\n'.join(out)

        def summ(level):
            parts = []
            for t in tops:
                if t in topics and topics[t][level]:
                    anc = json.loads(topics[t]['anchors'] or '[]')
                    parts.append(f'[topic {topics[t]["name"]} / {level}] {topics[t][level]}' + (f'\nanchors: {", ".join(anc)}' if anc else ''))
            return '\n\n'.join(parts)

        snip = topic_log(cfg.exact_snippet_tokens) if exact else ''
        base_ev = fact_block() + (('\n\n[original log snippets]\n' + snip) if snip else '')
        steps = [('small', lambda: summ('small'), lambda: base_ev),
                 ('medium', lambda: summ('medium'), lambda: base_ev)]
        if not exact:
            steps.append(('big', lambda: summ('big'), lambda: base_ev))
        steps += [('topic_log', lambda: summ('small'), lambda: fact_block() + '\n\n[original log of these topics]\n' + topic_log(cfg.topic_log_tokens)),
                  ('whole_log', lambda: summ('small'), lambda: fact_block() + '\n\n[original log of these topics]\n' + topic_log(cfg.topic_log_tokens)
                   + '\n\n[whole-history search]\n' + '\n'.join(f'[log:{r["id"]} {r["role"]}] {r["content"][:1500]}' for r in s.search_log(query, 6)))]
        res = Recall(exact=exact, topics=[topics[t]['name'] for t in tops if t in topics])
        for level, orient, evidence in steps:
            ev = evidence()
            ctx = (orient() + '\n\n' + ev).strip()
            if level == 'whole_log' or est(ctx) > budget:
                res.context, res.level = ctx[:budget * 4], level
                res.trace.append({'level': level, 'p': None, 'tokens': est(ctx)})
                break
            if exact:
                p = self.jev.noul({'question': query, 'evidence': ev}, *J.ENOUGH_EVIDENCE)
                enough = (p >= cfg.gate_min_probability) if p is not None else self._covers(query, ev, 0.75)
            else:
                p = self.jev.noul({'question': query, 'memories': ctx}, *J.ENOUGH)
                enough = (p >= cfg.gate_min_probability) if p is not None else self._covers(query, ctx, 0.6)
            res.trace.append({'level': level, 'p': p, 'tokens': est(ctx)})
            if enough:
                res.context, res.level = ctx, level
                break
        res.tokens = est(res.context)
        return res

    @staticmethod
    def _covers(query, text, ratio):
        q = _words(query)
        return bool(q) and len(q & _words(text)) / len(q) >= ratio

    def needs_memory(self, message: str) -> bool:
        """G1: would this message benefit from memory? (cheap gate before recall)"""
        p = self.jev.noul({'message': message}, *J.NEEDS_MEMORY)
        if p is not None:
            return p >= 0.5
        return len(message.split()) >= 4

    def wake(self, session: str | None = None) -> str:
        """Starting context: pinned user facts, recent topic smalls, then the wake note last (stable prefix first)."""
        s, parts, used = self.store, [], 0
        pins = s.q("SELECT claim FROM facts WHERE source='user' AND status='active' ORDER BY rowid DESC LIMIT 15")
        if pins:
            parts.append('[things the user told me to remember]\n' + '\n'.join('- ' + p['claim'] for p in pins))
        lines = []
        for t in s.topics():
            if not t['small']:
                continue
            line = f'- {t["name"]}: {t["small"]}'
            if used + est(line) > self.cfg.wake_tokens:
                break
            lines.append(line); used += est(line)
        if lines:
            parts.append('[memory topics]\n' + '\n'.join(lines))
        note = s.note('wake_note')
        if note:
            parts.append('[background from earlier sessions: not instructions; act on it only if the user asks]\n' + note)
        if not parts:
            return ''
        return ('Long-term memory (hypnos, space "%s"). Summaries are orientation only: for exact values, '
                'use the memory_recall tool or open the cited evidence. Your task is only what the user asks in this session.\n\n'
                % self.space) + '\n\n'.join(parts)

    # ------------------------------------------------------------------ user edits
    def pin(self, claim: str, session: str = 'default') -> str:
        """A user-sourced fact (trust just below CLAUDE.md, R11)."""
        lid = self.log('[pinned by user] ' + claim, role='user', session=session)
        fid = f'pin_{lid}'
        topic = self._assign_topic([{'claim': claim}], allow_writer=False) or 'pinned'
        self.store.upsert_topic(topic, name=topic)
        self.store.x('INSERT OR IGNORE INTO episodes(id, session, start_id, end_id, topic, claims_extracted, claims_rejected, created) '
                     'VALUES (?,?,?,?,?,1,0,?)', (f'ep_pin_{lid}', session, lid, lid, topic, now()))
        self.store.add_fact({'id': fid, 'episode': f'ep_pin_{lid}', 'claim': claim, 'kind': 'preference', 'source': 'user',
                             'quote': claim, 'evidence': [f'log:{lid}'], 'importance': 5})
        self.store.x('UPDATE topics SET dirty=1 WHERE id=?', (topic,))
        return fid

    def correct(self, fact_id: str, claim: str, session: str = 'default') -> str:
        new = self.pin(claim, session)
        self.store.x("UPDATE facts SET status='superseded', superseded_by=?, valid_to=? WHERE id=?", (new, now(), fact_id))
        self.store.x('INSERT INTO links(src, dst, type, note) VALUES (?,?,?,?)', (new, fact_id, 'supersedes', 'user correction'))
        return new

    def forget(self, fact_id: str | None = None, query: str | None = None, hard: bool = False) -> int:
        """Archive facts (R5). hard=True also tombstones the cited log rows (privacy path, R1)."""
        rows = self.store.q('SELECT * FROM facts WHERE id=?', (fact_id,)) if fact_id else self.store.search_facts(query or '', 5)
        for f in rows:
            self.store.x("UPDATE facts SET status='archived' WHERE id=?", (f['id'],))
            self.store.x('DELETE FROM facts_fts WHERE fid=?', (f['id'],))
            if hard:
                for ev in json.loads(f['evidence'] or '[]'):
                    if ev.startswith('log:'):
                        self.store.tombstone(int(ev[4:].split('-')[0]), 'user asked to forget')
        return len(rows)

    # ------------------------------------------------------------------ review and cleanup (v0.9)
    def review(self, topic: str | None = None, limit: int = 200) -> list[dict]:
        """Active facts for the user to look through, newest first, with the ids that correct/forget take."""
        sql = ("SELECT f.id, f.claim, f.kind, f.source, f.valid_from, f.recalled, COALESCE(t.name, e.topic, '') AS topic "
               "FROM facts f LEFT JOIN episodes e ON e.id = f.episode LEFT JOIN topics t ON t.id = e.topic WHERE f.status='active'")
        args = ()
        if topic:
            sql += ' AND (e.topic = ? OR t.name = ?)'; args = (topic, topic)
        return [dict(r) for r in self.store.q(sql + ' ORDER BY f.rowid DESC LIMIT ?', args + (limit,))]

    def cleanup_suggestions(self, unused_days: int = 30) -> dict:
        """What could go: near-duplicate facts (the older copy), facts never recalled for a month, stale facts,
        and how much archived or replaced history a purge would remove. Nothing is deleted here."""
        s = self.store
        facts = s.q("SELECT f.*, e.topic AS tp FROM facts f LEFT JOIN episodes e ON e.id = f.episode WHERE f.status='active' ORDER BY f.rowid")
        dup, by_topic = [], defaultdict(list)
        for f in facts:
            fw = _words(f['claim'])
            for g in by_topic[f['tp']]:
                gw = _words(g['claim'])
                if fw and gw and len(fw & gw) / len(fw | gw) >= 0.6:
                    dup.append({'remove': g['id'], 'claim': g['claim'], 'kept': f['id'], 'kept_claim': f['claim']})
                    break
            by_topic[f['tp']].append(f)
        cutoff = now()[:10]
        from datetime import date, timedelta
        cutoff = (date.fromisoformat(cutoff) - timedelta(days=unused_days)).isoformat()
        unused = [{'id': f['id'], 'claim': f['claim'], 'date': (f['valid_from'] or '')[:10]} for f in facts
                  if f['source'] != 'user' and not f['recalled'] and (f['valid_from'] or '9')[:10] < cutoff]
        stale = [{'id': f['id'], 'claim': f['claim']} for f in facts if self.profile.stale(f)]
        c = lambda sql: s.q(sql)[0][0]
        return {'duplicates': dup, 'unused': unused, 'stale': stale,
                'purgeable': {'archived': c("SELECT COUNT(*) FROM facts WHERE status='archived'"),
                              'replaced': c("SELECT COUNT(*) FROM facts WHERE status='superseded'")}}

    def forget_ids(self, ids: list[str], hard: bool = False) -> int:
        """Forget exactly these facts (from review or cleanup_suggestions), all in one transaction."""
        s, ids = self.store, [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return 0
        rows = []
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            rows += s.q(f"SELECT id, evidence FROM facts WHERE status='active' AND id IN ({','.join('?' * len(chunk))})", chunk)
        stmts = []
        for f in rows:
            stmts += [("UPDATE facts SET status='archived' WHERE id=?", (f['id'],)), ('DELETE FROM facts_fts WHERE fid=?', (f['id'],))]
            if hard:
                for ev in json.loads(f['evidence'] or '[]'):
                    if ev.startswith('log:'):
                        lid = int(ev[4:].split('-')[0])
                        stmts += [("UPDATE log SET content='', tombstone=? WHERE id=?",
                                   (json.dumps({'deleted': now(), 'reason': 'user asked to forget'}), lid)),
                                  ("UPDATE facts SET evidence_status='deleted' WHERE evidence LIKE ?", (f'%log:{lid}%',))]
        s.batch(stmts)
        return len(rows)

    # ------------------------------------------------------------------ /memory-cleanup, fast (v0.9)
    def cleanup_view(self, unused_days: int = 30, per_list: int = 15) -> dict:
        """Everything /memory-cleanup shows, already numbered and written out, so the agent only has to print it.
        Suggestions are numbered 1, 2, 3...; topics are T1, T2...; `map` turns those numbers back into fact ids."""
        s = self.store
        sug = self.cleanup_suggestions(unused_days)
        lines, nums, n = [], {}, 0

        def section(title, items, text):
            nonlocal n
            if not items:
                return
            lines.append(f'{title} ({len(items)}):')
            for it in items[:per_list]:
                n += 1
                nums[str(n)] = [it.get('remove') or it.get('id')]
                lines.append(f'  {n}. {text(it)[:150]}')
            if len(items) > per_list:
                lines.append(f'  ... {len(items) - per_list} more (say "all {title.split()[0].lower()}" to pick them all)')
            nums['all ' + title.split()[0].lower()] = [it.get('remove') or it.get('id') for it in items]
        section('Duplicates', sug['duplicates'], lambda d: f'{d["claim"]}  (same as: {d["kept_claim"][:60]})')
        section('Unused', sug['unused'], lambda u: f'{u["claim"]}  (since {u["date"]})')
        section('Stale', sug['stale'], lambda x: x['claim'])
        rows = s.q("SELECT COALESCE(t.id, e.topic, 'inbox') AS tid, COALESCE(t.name, e.topic, 'inbox') AS name, f.id AS fid "
                   "FROM facts f LEFT JOIN episodes e ON e.id = f.episode LEFT JOIN topics t ON t.id = e.topic "
                   "WHERE f.status='active' ORDER BY f.rowid DESC")
        topics = {}
        for r in rows:
            topics.setdefault(r['tid'], {'name': r['name'], 'ids': []})['ids'].append(r['fid'])
        if topics:
            lines.append(f'Topics ({len(rows)} facts):')
            for k, (tid, tp) in enumerate(sorted(topics.items(), key=lambda kv: -len(kv[1]['ids'])), 1):
                nums[f't{k}'] = tp['ids']
                lines.append(f'  T{k}. {tp["name"]}: {len(tp["ids"])} facts')
        pg = sug['purgeable']
        if pg['archived'] or pg['replaced']:
            lines.append(f'Already forgotten: {pg["archived"]}, replaced versions: {pg["replaced"]} (say "purge" to delete them for good)')
        head = f'Memory for {self.space}: {len(rows)} facts.'
        if not lines:
            return {'text': head + ' Nothing to clean up.', 'facts': len(rows), 'map': nums}
        return {'text': head + '\n' + '\n'.join(lines), 'facts': len(rows), 'map': nums}

    def remember_cleanup_map(self, view: dict):
        """Save the numbers of the list the user is looking at, so cleanup_apply deletes exactly what they saw."""
        self.store.note('cleanup_map', json.dumps(view.get('map') or {}))

    def cleanup_apply(self, selection: str, hard: bool = False) -> dict:
        """Delete what the user picked from the last cleanup_view: "1-5, 9", "T2", "all duplicates", "show T3" ...
        Returns what was removed; "show Tn" returns that topic's facts instead."""
        nums = json.loads(self.store.note('cleanup_map') or '{}')
        sel = selection.lower().replace(' and ', ',')
        m = re.match(r'\s*(?:show|list|open)\s+(t\d+)', sel)
        if m:
            ids = nums.get(m.group(1), [])
            rows = self.store.q(f"SELECT id, claim FROM facts WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else []
            return {'show': [f'{r["claim"]}' for r in rows][:200]}
        ids = []
        for key in ('all duplicates', 'all unused', 'all stale'):
            if key in sel:
                ids += nums.get(key, []); sel = sel.replace(key, '')
        for part in re.split(r'[,\s]+', sel):
            a = re.match(r'^(t?)(\d+)(?:-(\d+))?$', part)
            if not a:
                continue
            lo, hi = int(a.group(2)), int(a.group(3) or a.group(2))
            for k in range(lo, hi + 1):
                ids += nums.get(f'{a.group(1)}{k}', [])
        n = self.forget_ids(ids, hard)
        if 'purge' in selection.lower():
            self.purge()
        left = self.store.q("SELECT COUNT(*) FROM facts WHERE status='active'")[0][0]
        return {'forgotten': n, 'facts_left': left}

    def purge(self, replaced: bool = False) -> dict:
        """Delete archived facts for good (and replaced ones too if asked), with their search entries; shrink the file."""
        s = self.store
        statuses = ('archived', 'superseded') if replaced else ('archived',)
        ids = [r['id'] for r in s.q(f"SELECT id FROM facts WHERE status IN ({','.join('?' * len(statuses))})", statuses)]
        for i in ids:
            s.x('DELETE FROM facts WHERE id=?', (i,))
            s.x('DELETE FROM facts_fts WHERE fid=?', (i,))
            s.x('DELETE FROM fact_entities WHERE fid=?', (i,))
            s.x("DELETE FROM vectors WHERE kind='fact' AND ref=?", (i,))
        for t in s.q("SELECT id FROM topics WHERE status='active'"):
            if not s.facts_of_topic(t['id']):
                s.x("UPDATE topics SET status='archived' WHERE id=?", (t['id'],))
        with s.lock:
            s.db.execute('VACUUM')
        return {'deleted': len(ids)}

    # ------------------------------------------------------------------ asleep
    def sleep(self, session: str | None = None, max_episodes: int = 50) -> dict:
        """Consolidate everything after the watermark: segment, extract, verify, link to topics, rebuild dirty topics, wake note."""
        s = self.store
        rows = s.unconsolidated(session)
        report = {'log_rows': len(rows), 'episodes': 0, 'claims': 0, 'kept': 0, 'topics_rebuilt': 0}
        by_session = defaultdict(list)
        for r in rows:
            by_session[r['session']].append(r)
        plan = []
        for sess, srows in by_session.items():
            windows, cur, size = [], [], 0
            for r in srows:
                line = f'[#{r["id"]} {r["role"]}] {r["content"][:4000]}'
                if cur and size + len(line) > self.cfg.episode_chars:
                    windows.append(cur); cur, size = [], 0
                cur.append((r, line)); size += len(line)
            if cur:
                windows.append(cur)
            plan += [(sess, win) for win in windows]
        plan = plan[:max_episodes]
        # v0.9 speed: the slow part is the writer model, so the episodes' extractions run at once (writer_parallel at a
        # time); storing then goes episode by episode in the original order, so the result is the same as one at a time.
        with ThreadPoolExecutor(max(1, self.cfg.writer_parallel)) as ex:
            raws = ex.map(lambda sw: self._safe(self._extract, sw[1]), plan)
            for (sess, win), raw in zip(plan, raws):
                if isinstance(raw, Exception):
                    raise raw                                             # the watermark stays: the next sleep retries
                kept, claims = self._consolidate_episode(sess, win, raw=raw)
                report['claims'] += claims; report['kept'] += kept; report['episodes'] += 1
                s.set_watermark(sess, win[-1][0]['id'])
        dirty = [d['id'] for d in s.q('SELECT id FROM topics WHERE dirty=1')]
        with ThreadPoolExecutor(max(1, self.cfg.writer_parallel)) as ex:  # topic summaries and the wake note at once
            jobs = [ex.submit(self._rebuild_topic, tid) for tid in dirty]
            if report['episodes']:
                jobs.append(ex.submit(self._wake_note))
            for j in jobs:
                j.result()
        report['topics_rebuilt'] = len(dirty)
        if report['episodes']:
            self.index_log()                                              # v0.7: meaning search over raw messages
        return report

    @staticmethod
    def _safe(fn, *a):
        try:
            return fn(*a)
        except Exception as ex:
            return ex

    STATE_KINDS = {'preference', 'personal_fact', 'project', 'plan', 'account_state', 'object_location', 'map', 'calibration',
                   'convention', 'command', 'selector', 'site_howto', 'decision'}

    def _extract(self, win, only_row=None):
        text = '\n'.join(line for _, line in win)
        return self.writer.complete(
            'The attached file is one part of a log of work between a user and an AI agent. '
            f'List the facts worth remembering for future sessions: {self.profile.focus}. Up to 12 facts'
            + (' stated in the LAST message only (earlier lines are context)' if only_row else '') + '. '
            'Each fact: "claim" (one self-contained sentence with enough context to be understood later; name people and things '
            f'explicitly, no pronouns), "kind" (one of: {", ".join(self.profile.kinds)}), "quote" (a short passage under 200 characters '
            'copied EXACTLY from the log that supports the claim), "entities" (the specific people, places, projects, tools, files, '
            'devices or objects the fact is about, as short names). Return ONLY a JSON array.', [text])

    def _consolidate_episode(self, session, win, ep_id=None, only_row=None, allow_writer_topic=True, raw=None):
        s = self.store
        ep_id = ep_id or f'ep_{session}_{win[0][0]["id"]}'
        if raw is None:
            raw = self._extract(win, only_row)
        items = [x for x in (parse_json(raw) or []) if isinstance(x, dict) and x.get('claim')]
        kept, found = [], []
        for i, x in enumerate(items):
            quote = str(x.get('quote', '')).strip()
            cands = [(r, line) for r, line in win if only_row is None or r['id'] == only_row]
            hit = next((r for r, line in cands if quote and quote[:100].lower() in (r['content'] or '').lower()), None)
            if hit is None:                                                   # R2/R3: no evidence -> not a fact
                continue
            if is_refusal(str(x['claim'])):                                   # v0.9 filter: an AI refusing is not a fact
                continue
            found.append((i, x, quote, hit))
        ps = self.jev.verify_many([{'claim': f[1]['claim'], 'evidence': f[3]['content'][:2500]} for f in found],
                                  *J.VERIFY)                                  # v0.9: all claims at once
        for (i, x, quote, hit), p in zip(found, ps):
            if p is not None and p < 0.5:
                continue
            meta = json.loads(hit['meta'] or '{}')
            evidence = [f'log:{hit["id"]}'] + [e for e in meta.get('evidence', []) if isinstance(e, str)]
            ents = [str(e)[:80] for e in (x.get('entities') or []) if isinstance(e, (str, int, float)) and str(e).strip()][:8]
            kept.append({'id': f'{ep_id}#f{i + 1}', 'episode': ep_id, 'claim': str(x['claim'])[:500],
                         'kind': x.get('kind') if x.get('kind') in self.profile.kinds else self.profile.kinds[-1],
                         'source': 'user' if hit['role'] in ('user', 'fact') and self.profile.name in ('chat', 'generic') else 'observed',
                         'quote': quote[:300], 'evidence': evidence, 'verify_p': p, 'entities': ents, 'valid_from': hit['ts'],
                         'scope': meta.get('scope', {}), 'meta': {k: meta[k] for k in ('cwd', 'url', 'sensor') if k in meta}})
        kept = [f for f in kept if not self._duplicate(f)]
        topic = self._assign_topic(kept, allow_writer=allow_writer_topic) if kept else None
        if kept and not topic:
            topic = 'inbox'                                                    # real-time facts without a topic yet
            s.upsert_topic(topic, name='inbox', desc='recent facts not yet sorted into a topic')
        if only_row is None or kept:
            s.x('INSERT OR REPLACE INTO episodes(id, session, start_id, end_id, topic, claims_extracted, claims_rejected, created) '
                'VALUES (?,?,?,?,?,?,?,?)', (ep_id, session, (only_row or win[0][0]['id']), (only_row or win[-1][0]['id']), topic,
                                             len(items), len(items) - len(kept), now()))
        stored = []
        for f in kept:
            if self._resolve_contradictions(f):
                s.add_fact(f); stored.append(f)
        if topic and stored:
            s.x('UPDATE topics SET dirty=1 WHERE id=?', (topic,))
        self.vec.index('fact', [(f['id'], f['claim'] + ' ' + f['quote']) for f in stored])
        return len(stored), len(items)

    def _duplicate(self, f) -> bool:
        """Same evidence row and mostly the same words -> already stored (real-time path, then sleep)."""
        lid = f['evidence'][0]
        fw = _words(f['claim'])
        for g in self.store.q("SELECT claim FROM facts WHERE status!='archived' AND evidence LIKE ?", (f'%"{lid}"%',)):
            gw = _words(g['claim'])
            if fw and gw and len(fw & gw) / len(fw | gw) >= 0.5:
                return True
        return False

    def _resolve_contradictions(self, new) -> bool:
        """J6 with validity windows. Returns False if the new fact should not be stored (older still valid)."""
        s = self.store
        cands = {}
        for e in new.get('entities', []):
            for g in s.facts_with_entities([entity_id(e)], k=4):
                cands[g['id']] = g
        for g in s.search_facts(new['claim'], 4):
            cands[g['id']] = g
        nw = _words(new['claim'])
        olds = [g for g in list(cands.values())[:4] if g['episode'] != new['episode'] and g['claim'] != new['claim']]

        def judge(g):
            return self.jev.choice(
                {'older': {'claim': g['claim'], 'date': (g['valid_from'] or '')[:10]},
                 'newer': {'claim': new['claim'], 'date': (new.get('valid_from') or '')[:10]}},
                'Compare the older fact in `older` with the newer fact in `newer`. Are they about the same thing, and if so, which holds now?',
                {'newer_replaces': 'Same thing; the newer fact replaces or updates the older one.',
                 'both_true': 'Related but both can be true at once (different context, time or detail).',
                 'older_still_valid': 'Same thing; the newer statement is mistaken or less reliable, the older one still holds.',
                 'unrelated': 'They are about different things.'}, role='judge')
        if len(olds) > 1:                                                    # v0.9: all candidates at once
            with ThreadPoolExecutor(len(olds)) as ex:
                verdicts = list(ex.map(judge, olds))
        else:
            verdicts = [judge(g) for g in olds]
        for g, ch in zip(olds, verdicts):
            if ch is None:                                                   # no Jev: conservative plain-code rule
                gw = _words(g['claim'])
                same = (new['kind'] == g['kind'] and new['kind'] in self.STATE_KINDS and nw and gw
                        and len(nw & gw) / len(nw | gw) >= 0.45)
                ch = 'newer_replaces' if same and (new.get('valid_from') or '') >= (g['valid_from'] or '') else 'unrelated'
            if ch == 'older_still_valid':
                return False
            if ch == 'newer_replaces':
                if g['source'] == 'user' and new['source'] != 'user':        # R11: lower trust never silently replaces higher trust
                    s.x('INSERT INTO links(src, dst, type, note) VALUES (?,?,?,?)', (new['id'], g['id'], 'contradicts', 'lower-trust conflict'))
                    continue
                s.x("UPDATE facts SET status='superseded', superseded_by=?, valid_to=? WHERE id=?",
                    (new['id'], new.get('valid_from') or now(), g['id']))
                s.x('DELETE FROM facts_fts WHERE fid=?', (g['id'],))
                s.x('INSERT INTO links(src, dst, type, note) VALUES (?,?,?,?)', (new['id'], g['id'], 'supersedes', ''))
        return True

    def _assign_topic(self, facts, allow_writer=True):
        s = self.store
        existing = s.topics()
        claims = [f['claim'] for f in facts]
        if existing:
            opts = {t['id']: (t['desc'] or t['name'])[:200] for t in existing[:250]}
            opts['new_topic'] = 'None of the existing topics fits these facts well.'
            c = self.jev.choice({'facts': claims}, 'Which existing memory topic do the facts in `facts` mainly belong to? Pick new_topic if none fits well.', opts)
            if c and c != 'new_topic':
                return c
            if c is None:                                                    # no Jev: best keyword overlap
                fw = _words(' '.join(claims))
                best = max(existing, key=lambda t: len(fw & _words((t['name'] or '') + ' ' + (t['desc'] or '') + ' ' + (t['small'] or ''))))
                if len(fw & _words((best['name'] or '') + ' ' + (best['desc'] or '') + ' ' + (best['small'] or ''))) >= 3:
                    return best['id']
        if not allow_writer:
            return None
        listing = '\n'.join(f'- {t["id"]}: {t["desc"]}' for t in existing[:80]) or '(none yet)'
        r = parse_json(self.writer.complete(
            'Existing memory topics (id: description):\n' + listing + '\n\nFacts from a new episode are in the attached file. '
            'Pick the ONE existing topic these facts mainly belong to, or create a new topic if none fits well. '
            'Topics should be about a subject (a project, device, tool, person, place, problem area), not a date. '
            'Return ONLY JSON: {"topic": "<existing id or new_short_snake_case_name>", "new": true|false, "desc": "<one-line description if new>"}',
            ['\n'.join('- ' + c for c in claims)])) or {}
        tid = re.sub(r'[^a-z0-9_]+', '_', str(r.get('topic', 'general')).lower()).strip('_')[:40] or 'general'
        if not s.q('SELECT 1 FROM topics WHERE id=?', (tid,)):
            s.upsert_topic(tid, name=tid, desc=str(r.get('desc', tid))[:200])
        return tid

    def _rebuild_topic(self, tid):
        facts = self.store.facts_of_topic(tid)[-80:]
        if not facts:
            self.store.x('UPDATE topics SET dirty=0 WHERE id=?', (tid,))
            return
        t = self.store.q('SELECT * FROM topics WHERE id=?', (tid,))[0]
        src = '\n'.join(f'[{f["id"]}] ({f["kind"]}) {f["claim"]}' for f in facts)
        specs = (('small', self.cfg.topic_small_tokens, 'one or two sentences'),
                 ('medium', self.cfg.topic_medium_tokens, 'a short paragraph or a few bullets'),
                 ('big', self.cfg.topic_big_tokens, 'a detailed structured summary'))

        def write(spec):
            level, tokens, shape = spec
            return self.writer.complete(
                f'Write the {level.upper()} memory summary for the topic "{t["name"]}" from the verified facts in the attached file. '
                f'Size: about {tokens} tokens, {shape}. Keep the most useful specifics (values, paths, commands, decisions, outcomes). '
                'Use only these facts. Return only the summary text.', [src])
        with ThreadPoolExecutor(min(3, max(1, self.cfg.writer_parallel))) as ex:   # v0.9: the three sizes at once
            levels = dict(zip([sp[0] for sp in specs], ex.map(write, specs)))
        self.store.upsert_topic(tid, anchors=anchors_for(facts, self.cfg.anchors_max), dirty=0, **levels)
        self.vec.index('topic', [(f'{tid}:{lvl}', txt) for lvl, txt in levels.items()])

    def _wake_note(self):
        recent = self.store.q("SELECT claim FROM facts WHERE status='active' ORDER BY rowid DESC LIMIT 40")
        if not recent:
            return
        note = self.writer.complete(
            'From the recent facts in the attached file, write a short wake note for a later session: open problems and '
            'unfinished work, written as background ("X was left unfinished"), never as instructions or a to-do list, because '
            'the next session may have a different task. At most 8 bullet points. Return only the bullets.',
            ['\n'.join('- ' + r['claim'] for r in recent)])
        self.store.note('wake_note', note[:2000])

    # ------------------------------------------------------------------ export / status
    def export(self) -> str:
        out = [f'# Memory: {self.space}\n']
        pins = self.store.q("SELECT id, claim FROM facts WHERE source='user' AND status='active'")
        if pins:
            out.append('## Pinned by the user\n' + '\n'.join(f'- {p["claim"]}  <!-- {p["id"]} -->' for p in pins))
        for t in self.store.topics():
            out.append(f'## {t["name"]}\n{t["small"]}\n\n{t["medium"]}')
        note = self.store.note('wake_note')
        if note:
            out.append('## Wake note\n' + note)
        return '\n\n'.join(out) + '\n'

    def status(self) -> dict:
        c = lambda sql: self.store.q(sql)[0][0]
        return {'space': self.space, 'profile': self.profile.name, 'db': str(self.store.path),
                'log_rows': c('SELECT COUNT(*) FROM log'), 'unconsolidated': len(self.store.unconsolidated()),
                'facts': c("SELECT COUNT(*) FROM facts WHERE status='active'"), 'topics': c("SELECT COUNT(*) FROM topics WHERE status='active'"),
                'jev': self.jev.enabled, 'vectors': self.vec.enabled, 'writer': self.cfg.writer}
