"""Jev (TypeSafe) client for the gate and judge questions. Returns None when Jev is unavailable,
so every caller has a plain-code fallback and the engine works without a key."""
from __future__ import annotations
import json
import os
import threading
import urllib.request

API = 'https://api.typesafe.ai/v1/systemone'
LAYA_API = 'http://127.0.0.1:8000/v1/systemone'          # v0.9: `laya-serve` on this machine
# v0.9: many agent sessions share one service; cap the Jev requests in flight across all of them
IN_FLIGHT = threading.BoundedSemaphore(int(os.environ.get('HYPNOS_JEV_MAX_IN_FLIGHT', '24')))


class Jev:
    def __init__(self, cfg, store=None):
        self.store = store
        self.cfg = cfg
        # v0.9: no System One model -> the cheap LLM chosen at setup answers the checks made while saving (is this
        # claim supported, does it replace an older fact, which topic). More accurate than plain rules, costs tokens.
        # Recall stays on plain code (no LLM call per message), so replies are not slowed down.
        self.llm = getattr(cfg, 'systemone', 'jev') == 'llm'
        self.routing = not self.llm
        if self.llm:
            self.enabled, self.key, self.laya, self.url, self._writer = bool(cfg.jev), '', False, '', None
            return
        self.laya = getattr(cfg, 'systemone', 'jev') == 'laya'
        self.url = getattr(cfg, 'systemone_url', '') or (LAYA_API if self.laya else API)
        if self.laya:                          # local Laya: a key only if laya-serve was started with LAYA_API_KEY
            self.key = os.environ.get('LAYA_API_KEY', '')
            self.enabled = bool(cfg.jev)
            return
        self.key = os.environ.get('TYPESAFE_API_KEY', '')
        if not self.key and cfg.jev and cfg.jev_credentials.exists():
            for line in cfg.jev_credentials.read_text(encoding='utf-8').splitlines():
                if line.startswith('TYPESAFE_API_KEY='):
                    self.key = line.split('=', 1)[1].strip()
        self.enabled = bool(cfg.jev and self.key)

    def ask(self, state, questions, role='gate', timeout=15):
        if not self.enabled:
            return None
        if self.llm:                     # only the topic choice; claims are checked in one batch (verify_many)
            topic = any(isinstance(q.get('criteria'), dict) and 'new_topic' in q['criteria'] for q in questions.values())
            return self._ask_llm(state, questions) if role == 'judge' and topic else None
        msg = {'state': state, 'questions': questions} if self.laya else {'state': state, 'model': 'jev-latest', 'questions': questions}
        headers = {'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + self.key} if self.key else {})}
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), method='POST', headers=headers)
        try:
            with IN_FLIGHT, urllib.request.urlopen(req, timeout=timeout) as r:
                answers = json.loads(r.read())['answers']
        except Exception:
            return None
        if self.store is not None:
            for qid, a in answers.items():
                self.store.decision(role, qid, a.get('choice', a.get('noul', a.get('score'))), a.get('noul', a.get('confidence')))
        return answers

    def _ask_llm(self, state, questions):
        """The same typed questions, answered by an LLM in one call. Returns Jev-shaped answers or None."""
        from .llm import make_writer, parse_json
        if self._writer is None:
            self._writer = make_writer(self.cfg)
        lines = []
        for qid, q in questions.items():
            crit = q.get('criteria')
            if q['type'] == 'noul':
                opts = 'answer with a probability from 0 to 1 that the answer is yes' + (
                    f' (yes means: {crit.get("true")}; no means: {crit.get("false")})' if isinstance(crit, dict) else '')
            else:
                opts = 'answer with exactly one of these option keys: ' + json.dumps(crit if isinstance(crit, dict) else {})
            lines.append(f'- "{qid}": {q["instructions"]} -- {opts}')
        try:
            raw = self._writer.complete(
                'Answer each question about the attached JSON state. Be strict and literal. Questions:\n' + '\n'.join(lines)
                + '\nReturn ONLY a JSON object mapping each question id to {"noul": <0..1>} or {"choice": "<option key>"}.',
                [json.dumps(state, ensure_ascii=False)[:8000]])
            got = parse_json(raw) or {}
        except Exception:
            return None
        out = {}
        for qid, q in questions.items():
            a = got.get(qid) if isinstance(got, dict) else None
            if not isinstance(a, dict):
                return None
            if q['type'] == 'noul':
                try:
                    out[qid] = {'noul': max(0.0, min(1.0, float(a.get('noul'))))}
                except (TypeError, ValueError):
                    return None
            else:
                c = str(a.get('choice', ''))
                if isinstance(q.get('criteria'), dict) and c not in q['criteria']:
                    return None
                out[qid] = {'choice': c}
        return out

    def verify_many(self, states, instructions, true, false):
        """P(supported) for each {claim, evidence} state. System One: one request each, in parallel.
        LLM mode: every claim in ONE call (a call per claim would cost far too many tokens)."""
        if not states:
            return []
        if not self.llm:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max(1, min(len(states), getattr(self.cfg, 'jev_parallel', 12)))) as ex:
                return list(ex.map(lambda s: self.noul(s, instructions, true, false, role='judge'), states))
        if not self.enabled:
            return [None] * len(states)
        qs = {f'c{i}': {'type': 'noul', 'instructions': f'{instructions} (Look only at item c{i} in the state.)',
                        'criteria': {'true': true, 'false': false}} for i in range(len(states))}
        a = self._ask_llm({f'c{i}': s for i, s in enumerate(states)}, qs)
        return [a[f'c{i}']['noul'] if a else None for i in range(len(states))]

    def noul(self, state, instructions, true, false, role='gate'):
        a = self.ask(state, {'q': {'type': 'noul', 'instructions': instructions, 'criteria': {'true': true, 'false': false}}}, role)
        return None if a is None else a.get('q', {}).get('noul')

    def many(self, state, questions: dict, role='gate'):
        """Several typed questions about the same state in ONE request (they run in parallel, nearly free).
        questions: {id: (type, instructions, criteria)}. Returns {id: answer dict} or None."""
        qs = {k: {'type': t, 'instructions': ins, 'criteria': crit} for k, (t, ins, crit) in questions.items()}
        return self.ask(state, qs, role)

    def choice(self, state, instructions, options: dict, role='judge'):
        a = self.ask(state, {'q': {'type': 'choice', 'instructions': instructions, 'criteria': options}}, role)
        return None if a is None else a.get('q', {}).get('choice')


# The typed questions used by the engine (section 6 of the architecture)
EXACT = ('Does the question in `question` ask for a specific exact value that must be reproduced precisely, '
         'such as a name, file path, number, ID, setting, version, command or quoted output string?',
         'The correct answer is a precise value that must match exactly.',
         'The question asks for an explanation, reason, summary or general description.')
ENOUGH_EVIDENCE = ('Does the evidence in `evidence` contain, word for word, the exact information the question in `question` asks for?',
                   'The exact value asked for appears verbatim in the evidence.',
                   'The exact value is missing, incomplete, or only paraphrased.')
ANSWERABLE = ('Does the evidence in `evidence` (facts and original messages) contain everything needed to answer the question in '
              '`question` completely? For questions asking for a list or a count, all items must be present.',
              'The evidence answers the question completely.',
              'Part of the answer is missing, or the evidence is only on the same subject.')
# v0.8 routing: what kind of question, and which summary level already answers it
INTENT = ('What kind of answer does the question in `question` need from a memory of past conversations?', {
    'overview': 'A general picture or gist: what someone was doing, working on, feeling or planning, in broad terms.',
    'single_fact': 'One specific fact: a name, thing, place, reason or yes/no.',
    'list': 'Several items or a count gathered from different moments (all X, how many X, what kinds of X).',
    'when': 'A date or time of an event.',
    'exact_value': 'A precise value that must match exactly: a file path, command, number, ID, setting or quoted text.'})
SUMMARY_ENOUGH = ('Does the summary in `summary` alone answer the question in `question` completely and specifically?',
                  'The summary answers the question completely.',
                  'The summary is missing the specific information asked for, or is only on the same subject.')
# v0.8 gating: one request per candidate (question + one memory item), several nouls about the pair
ITEM_RELEVANT = ('Is the memory in `memory` about the subject of the question in `question`?',
                 'It is about the same subject.', 'It is about something else.')
ITEM_USABLE = ('Does the memory in `memory` state information that is part of the answer to the question in `question`?',
               'It states part of the answer.', 'It does not contain any part of the answer.')
ENOUGH = ('Do the memories in `memories` contain the specific information the question in `question` asks for, so it can be answered precisely?',
          'The information asked for appears in the memories.',
          'The memories are on the right subject but the information asked for is missing or vague.')
VERIFY = ('Does the evidence text in `evidence` directly support the claim in `claim`? Judge only from that evidence.',
          'The evidence states or clearly shows what the claim says.',
          'The evidence does not show it, contradicts it, or the claim adds details not in the evidence.')
NEEDS_MEMORY = ('Would answering or acting on the message in `message` benefit from recalling earlier sessions with this user '
                '(past decisions, preferences, facts, work done before)?',
                'Earlier context would likely help.',
                'The message is self-contained or small talk.')
