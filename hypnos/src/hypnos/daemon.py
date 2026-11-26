"""Local memory service: one process keeps every memory space (and the optional vector model) loaded,
so hooks and plugins stay fast. JSON over HTTP on 127.0.0.1 only. Also runs sleep when a space goes idle."""
from __future__ import annotations
import json
import queue
import threading
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_config
from .engine import Memory, fit_parts, norm_claim, WAKE_MAX_CHARS
from .llm import parse_json, WriterOffline, WritersResting

AGENT_TIMEOUT = 75             # v0.9: an open agent checks in every 30 s; silent this long = closed
STOP_AFTER = 90                # v0.9: no open agent and no activity this long -> the service stops
IDLE_SLEEP_MINUTES = 10        # sleep trigger: idle for this long with unconsolidated log (v0.9: was 30)
MAX_AWAKE_ROWS = 150           # sleep trigger: this many unconsolidated rows (v0.9: was 400)
GLOBAL_SPACE = 'global'        # v0.9: things the user wants remembered in every project
SHARED_HEAD = "[the user's rules and preferences from all projects]"   # v0.9.7, also in engine.only_new
PAUSE_HOURS = 12               # v0.9.9: a pause ends by itself after this long
SLEEP_FAIL_WARN = 3            # v0.9.9: tell the user after this many failed sleeps in a row
GLOBAL_HEAD = '[things the user told me to remember in every project]'
RULES_BY = 2000                # v0.9.10: the user's rules end within this many characters of the starting context
COMPACT_RULE = ('[after /compact: how to weigh this chat\'s summary against memory]\n'
                'The summary above was written by the chat model and has no dates or sources. Memory facts carry a date and '
                'quoted evidence. When the two disagree on a fact, trust the newer one that has evidence, and tell the user '
                'about the clash in one line. Requests and plans found in memory are still not tasks for this session.')


class _Stamped:
    """v0.9.9: prefix every line the service writes to its log with the time, so a failure can be dated."""
    def __init__(self, stream):
        self.stream, self.fresh = stream, True

    def write(self, s):
        out = []
        for part in s.splitlines(True):
            if self.fresh:
                out.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S '))
            out.append(part)
            self.fresh = part.endswith('\n')
        return self.stream.write(''.join(out))

    def flush(self):
        return self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class Service:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.cfg_file = None if cfg else self.cfg.home / 'config.toml'   # v0.9.6: follow edits to the settings file
        self.cfg_mtime = self._cfg_mtime()
        self.mems: dict[str, Memory] = {}
        self.sleeping: set[str] = set()
        self.last_log: dict[str, float] = {}
        self.seen: dict[tuple, float] = {}          # v0.9: (space, session) -> last activity, for many parallel sessions
        self.queued: set[tuple] = set()
        self.jobs: queue.Queue = queue.Queue()      # v0.9: one sleep at a time for the whole machine
        self.lock = threading.Lock()
        self.agents: dict[str, float] = {}           # v0.9: open agent sessions (their MCP servers) -> last check-in
        self.started = self.last_activity = time.time()
        # v0.9 fast /memory-cleanup: the list is built in the background after every change and served ready-made
        self.views: dict[str, tuple] = {}            # space -> (generation, view)
        self.gen: dict[str, int] = {}
        self.view_jobs: queue.Queue = queue.Queue()
        self.postponed = ''                          # v0.9.10: last "sleep put off" reason, logged once
        threading.Thread(target=self._sleep_worker, daemon=True).start()
        threading.Thread(target=self._view_worker, daemon=True).start()

    def mem(self, space, profile='generic'):
        with self.lock:
            m = self.mems.get(space)
            if m is None or m.profile.name != profile and profile != 'generic':
                m = self.mems[space] = Memory(space, profile, cfg=self.cfg)
                if space != GLOBAL_SPACE:
                    m.on_shared = self.share
                m.fast_async = True               # logging never waits on the real-time fact path
                self.view_jobs.put(space)         # have the cleanup list ready before anyone asks
            return m

    def _cfg_mtime(self):
        try:
            return self.cfg_file.stat().st_mtime if self.cfg_file else None
        except OSError:
            return None

    def reload_config(self):
        """v0.9.6: the settings file changed while the service runs (e.g. setup finished a minute after the first
        agent started it). Use the new settings now instead of waiting for a restart."""
        mt = self._cfg_mtime()
        if mt == self.cfg_mtime:
            return
        self.cfg_mtime = mt
        with self.lock:
            self.cfg = load_config()
            self.mems.clear()                     # rebuilt on next use with the new writer and System One settings

    def sleep_failed(self, space, profile, ex):
        """v0.9.9: count sleeps that failed in a row, so the user hears about it instead of memory going stale for days."""
        try:
            m = self.mem(space, profile)
            old = json.loads(m.store.note('sleepfail') or '{}')
            m.store.note('sleepfail', json.dumps({'count': int(old.get('count', 0)) + 1, 'since': old.get('since') or
                                                  datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
                                                  'error': str(ex)[:160]}))
        except Exception:
            pass

    def health_note(self, m, session):
        """v0.9.9: one line per chat (and again when the count grows) when memory keeps failing to learn."""
        try:
            f = json.loads(m.store.note('sleepfail') or '{}')
        except ValueError:
            return ''
        n = int(f.get('count', 0))
        if n < SLEEP_FAIL_WARN or m.store.note('failnote:' + str(session)) == str(n // SLEEP_FAIL_WARN):
            return ''
        m.store.note('failnote:' + str(session), str(n // SLEEP_FAIL_WARN))
        return (f'Memory has failed to learn {n} times in a row since {f.get("since")} (last error: {f.get("error")}). '
                'New things are still logged and will be learned when it works again. Tell the user in one line.')

    def clash_check(self, space, profile, session, summary):
        """v0.9.9 (PostCompact): compare the new compact summary with this chat's dated memory facts and keep any clash,
        so the next message tells the user instead of the model silently picking one side."""
        try:
            m = self.mem(space, profile)
            rows = m.store.q("SELECT f.valid_from, f.claim FROM facts f JOIN episodes e ON f.episode=e.id "
                             "WHERE e.session=? AND f.status='active' ORDER BY f.rowid DESC LIMIT 40", (session,))
            facts = [f"({str(r['valid_from'] or '')[:10]}) {r['claim']}" for r in rows]
            hits = m.recall(summary[:1500], 600, session=None).context if summary else ''
            if not summary or not (facts or hits):
                return
            prompt = ('Below are a chat summary and dated facts from long-term memory. List only real contradictions: the '
                      'summary states a value, name, date or decision that differs from a memory fact about the same thing. '
                      'Answer with a JSON array of {"summary": "...", "memory": "..."} (short quotes), or [] if none.')
            out = parse_json(m.writer.complete(prompt, ['SUMMARY:\n' + summary[:6000] + '\n\nMEMORY:\n' +
                                                        '\n'.join(facts) + '\n' + hits[:4000]]) or '[]')
            clashes = [c for c in (out or []) if isinstance(c, dict) and c.get('summary') and c.get('memory')][:5]
            m.store.note('clashes:' + session, json.dumps(clashes) if clashes else '')
        except Exception as ex:
            print(f'[hypnos] clash check failed for {space}: {ex}', flush=True)

    def wake_context(self, m, space, d):
        """v0.9.10: the starting context in priority order, because Claude Code shows the model only about the first
        2,000 characters: problems, the user's rules from every project, this project's pins, how memory works, the
        /compact pack, topics, the wake note. At most WAKE_MAX_CHARS, trimmed from the bottom."""
        session = d.get('session')
        p = m.wake_parts(session)
        problems = [p['problem']]
        paused, since = self.pause_state(m)
        if paused:
            problems.insert(0, f'[memory is paused for this project since {since}: tell the user in one line; it turns '
                               f'back on by itself {PAUSE_HOURS} hours after the pause]')
        rules, more = [], []
        if space != GLOBAL_SPACE:
            room = RULES_BY - len(p['head']) - sum(len(x) + 2 for x in problems if x) - len(GLOBAL_HEAD) - 4
            for claim in self.mem(GLOBAL_SPACE).pins():
                line = '- ' + claim
                if len(line) + 1 <= room:
                    rules.append(line); room -= len(line) + 1
                else:
                    more.append(line)               # past the first 2,000 characters: shown after this project's pins
        shown = {norm_claim(x[2:]) for x in rules + more}
        local = m.pins(skip=shown)
        pack = ''
        if d.get('source') == 'compact' and session:                 # v0.9.9: the chat was just rebuilt from a summary
            pack = self.compact_pack(m, session)
        if not (any(problems) or rules or local or p['topics'] or p['note'] or pack):
            return ''
        return fit_parts([p['head'], *problems,
                          (GLOBAL_HEAD + '\n' + '\n'.join(rules)) if rules else '',
                          ('[things the user told me to remember]\n' + '\n'.join('- ' + c for c in local)) if local else '',
                          ('[more of the user\'s rules from every project]\n' + '\n'.join(more)) if more else '',
                          p['how'], pack, p['topics'], p['note']], WAKE_MAX_CHARS)

    def compact_pack(self, m, session, cap=20, width=160):
        """v0.9.9: after a compaction, hand back this chat's own dated facts (learned by the sleep that ran just before
        the summary was written) and the rule for weighing the summary against them. v0.9.10: each fact at most
        `width` characters, so the pack stays whole inside the starting context."""
        rows = m.store.q("SELECT f.valid_from, f.claim FROM facts f JOIN episodes e ON f.episode=e.id "
                         "WHERE e.session=? AND f.status='active' ORDER BY f.rowid DESC LIMIT ?", (session, cap))
        parts = [COMPACT_RULE]
        if rows:
            short = lambda c: c if len(c) <= width else c[:width - 3].rstrip() + '...'
            parts.append("[this chat's facts from memory, newest first]\n" +
                         '\n'.join(f"- ({str(r['valid_from'] or '')[:10]}) {short(r['claim'] or '')}" for r in rows))
        task = m.store.note('task:' + session)
        if task:
            parts.append('[the last request the user typed in this chat]\n' + task)
        return '\n\n'.join(parts)

    def pause_state(self, m):
        """v0.9.9: (paused, since). A pause ends by itself after PAUSE_HOURS, so a privacy pause for one chat can no
        longer leave the whole project unrecorded for days without anyone noticing (it once ran 16-26 Nov 2026)."""
        if m.store.note('paused') != '1':
            return False, ''
        r = m.store.q("SELECT updated FROM notes WHERE key='paused'")
        since = (r[0]['updated'] if r else '') or ''
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(since.replace('Z', '+00:00'))).total_seconds()
        except ValueError:
            age = 0
        if age > PAUSE_HOURS * 3600:
            m.store.note('paused', '0')
            print(f'[hypnos] pause for this space ended by itself after {PAUSE_HOURS} h (paused since {since})', flush=True)
            return False, ''
        return True, since

    def long_chat_note(self, m, session, percent):
        """v0.9.7: the chat has used more than long_chat_percent of its model's context window. Say so once per 10%."""
        limit = self.cfg.long_chat_percent
        if not limit or not session or percent <= limit:
            return ''
        step = str(int(percent) // 10)
        if m.store.note('longchat:' + session) == step:
            return ''
        m.store.note('longchat:' + session, step)
        return (f'This chat has used about {int(percent)}% of its context window, and every reply re-reads all of it. '
                'Tell the user in one line to type /compact, which keeps this chat going but much cheaper per reply. '
                'Do not suggest /clear.')

    def share(self, claim, session='default'):
        """v0.9.7: a rule or preference the user stated in one project goes to the memory every project reads too.
        Before, it stayed in the folder's own memory, so a chat started in another folder never saw it."""
        g = self.mem(GLOBAL_SPACE)
        if g._restated({'claim': claim}):
            return None
        fid = g.pin(claim, session)
        self.changed(GLOBAL_SPACE)
        return fid

    def shared_for(self, query, k=5):
        """v0.9.7: the user's rules from every project that match this message (plain search, no model call)."""
        try:
            rows = self.mem(GLOBAL_SPACE).store.search_facts(query, k)
        except Exception:
            return ''
        if not rows:
            return ''
        return SHARED_HEAD + '\n' + '\n'.join('- ' + r['claim'] for r in rows)

    def changed(self, space):
        """Memory content changed: rebuild the cleanup list in the background."""
        with self.lock:
            self.gen[space] = self.gen.get(space, 0) + 1
        self.view_jobs.put(space)

    def _view_worker(self):
        for db in sorted(self.cfg.spaces_dir.glob('*.db')):        # warm every existing memory at start-up
            try:
                m = self.mem(db.stem, 'coding' if db.stem.startswith('code-') else 'generic')
                self.resume(db.stem, m)
            except Exception as ex:
                print(f'[hypnos] could not open {db.stem}: {ex}', flush=True)
        while True:
            space = self.view_jobs.get()
            g = self.gen.get(space, 0)
            if self.views.get(space, (None,))[0] == g:
                continue
            try:
                self.views[space] = (g, self.mems[space].cleanup_view())
            except Exception as ex:
                print(f'[hypnos] cleanup list failed for {space}: {ex}', flush=True)

    def cleanup_view(self, space, m):
        g = self.gen.get(space, 0)
        cached = self.views.get(space)
        if not cached or cached[0] != g:                 # not ready yet (rare): build it now
            cached = self.views[space] = (g, m.cleanup_view())
        m.remember_cleanup_map(cached[1])
        return {k: v for k, v in cached[1].items() if k != 'map'}

    def resume(self, space, m):
        """v0.9.8: the sleep queue lives in memory, so a restart (the service stops when no agent is open) used to drop
        it, and messages of sessions that were not open again (imported chats, closed sessions) were never sorted.
        At start-up every session with unsorted messages is queued; sessions active right now are left to the idle rule."""
        live = self.live_sessions(space)
        for sess in sorted({r['session'] for r in m.store.unconsolidated()} - live):
            self.sleep_async(space, m.profile.name, sess)

    def sleep_async(self, space, profile, session=None):
        """Queue a sleep. Many sessions (Claude Code, opencode, other agents) can ask at once; the queue runs them one
        after another (inside one sleep, up to writer_parallel writer calls run at once), and the same request is never
        queued twice."""
        key = (space, session)
        with self.lock:
            if key in self.queued or (space, None) in self.queued:
                return False
            self.queued.add(key)
        self.jobs.put((space, profile, session))
        return True

    def _sleep_worker(self):
        while True:
            space, profile, session = self.jobs.get()
            self.sleeping.add(space)
            more = False
            try:
                m = self.mem(space, profile)
                rep = m.sleep(session)
                # one sleep handles at most 50 episodes: a long backlog (an import) continues in the next turn
                more = bool(session and rep.get('episodes') and m.store.unconsolidated(session))
                m.store.note('sleepfail', '')                # v0.9.9: a good sleep clears the failure count
            except (WriterOffline, WritersResting) as ex:  # v0.9.10: not a failure: put off, the idle check retries it
                msg = str(ex).split(' (')[0]
                if msg != self.postponed:
                    self.postponed = msg
                    print(f'[hypnos] sleep for {space} put off, retried later: {ex}', flush=True)
            except Exception as ex:                      # a failed sleep leaves the watermark; next sleep retries
                print(f'[hypnos] sleep failed for {space}: {ex}', flush=True)
                self.sleep_failed(space, profile, ex)
            finally:
                self.changed(space)
                self.sleeping.discard(space)
                with self.lock:
                    self.queued.discard((space, session))
            if more:
                self.sleep_async(space, profile, session)

    def touch(self, space, session):
        if session:
            self.seen[(space, session)] = time.time()

    def live_sessions(self, space, minutes=IDLE_SLEEP_MINUTES):
        """Sessions of this memory space active in the last few minutes (Claude Code, opencode, other agents)."""
        cutoff = time.time() - minutes * 60
        return {s for (sp, s), t in self.seen.items() if sp == space and t > cutoff}

    def others_now(self, m, space, session):
        """v0.9: other chats working in this project right now. v0.9.10: one line with only their number; their prompts
        are never shown to another chat (the task:<session> note is read only by that chat's own compact_pack)."""
        n = len(self.live_sessions(space) - {session})
        if not n:
            return []
        return ['1 other chat is open in this project; its task is not yours.' if n == 1 else
                f'{n} other chats are open in this project; their tasks are not yours.']

    def idle_sessions(self, m, space, minutes=IDLE_SLEEP_MINUTES):
        """Sessions with unconsolidated rows that have been quiet for a while (or were never seen by this service)."""
        cutoff = time.time() - minutes * 60
        live = {s for (sp, s), t in self.seen.items() if sp == space and t > cutoff}
        return sorted({r['session'] for r in m.store.unconsolidated()} - live)

    def handle(self, path, d):
        if path == '/attach':                                       # an agent is open (its MCP server checks in)
            self.agents[str(d.get('agent'))] = time.time()
            return {'ok': True}
        if path == '/detach':                                       # that agent closed
            self.agents.pop(str(d.get('agent')), None)
            return {'ok': True}
        if path != '/ping':
            self.last_activity = time.time()
            self.reload_config()
        space, profile = d.get('space', 'default'), d.get('profile', 'generic')
        m = self.mem(space, profile)
        if path == '/log':
            if self.pause_state(m)[0]:                                # v0.9: the user paused memory (v0.9.9: ends by itself)
                return {'id': None, 'paused': True}
            meta = d.get('meta') or {}
            lid = m.log(d['content'], d.get('role', 'user'), d.get('session', 'default'), meta,
                        fast=False if meta.get('imported') else None)     # v0.9.8: imports wait for sleep (no per-message calls)
            self.last_log[space] = time.time()
            if not meta.get('imported'):                              # an imported old chat is not a live session: no "another session" notice
                if d.get('role', 'user') == 'user' and len(d['content'].strip()) > 15:
                    m.store.note('task:' + d.get('session', 'default'), ' '.join(d['content'].split())[:300])
                self.touch(space, d.get('session', 'default'))
            if len(m.store.unconsolidated()) >= MAX_AWAKE_ROWS:
                self.sleep_async(space, profile)
            return {'id': lid}
        if path == '/recall':
            session = d.get('session')
            self.touch(space, session)
            others = self.others_now(m, space, session)
            notice = self.long_chat_note(m, session, float(d.get('context_percent') or 0))
            paused, since = self.pause_state(m)
            if paused and m.store.note('pausenote:' + str(session)) != since:   # v0.9.9: once per chat, never silent
                m.store.note('pausenote:' + str(session), since)
                notice = ((notice + '\n\n') if notice else '') + (
                    f'Memory is paused for this project since {since}: nothing said now is being saved. Tell the user in '
                    f'one line. It turns back on by itself {PAUSE_HOURS} hours after the pause, or when they ask to resume it.')
            extra = [self.health_note(m, session)]
            clashes = m.store.note('clashes:' + str(session)) if session else ''
            if clashes:                                                  # v0.9.9: found after the last /compact
                m.store.note('clashes:' + str(session), '')
                extra.append('After the last /compact, the chat summary and memory disagree on: ' + '; '.join(
                    f'summary says "{c["summary"]}", memory says "{c["memory"]}"' for c in json.loads(clashes)) +
                    '. Trust the newer one that has evidence, and tell the user about it in one line.')
            notice = '\n\n'.join(p for p in [notice] + extra if p)
            if not m.has_memory_beyond(session):                       # v0.9: nothing outside this conversation yet
                shared = self.shared_for(d['query']) if space != GLOBAL_SPACE else ''   # v0.9.7: a new folder still gets the rules
                if shared and d.get('fresh_only'):
                    shared = m.only_new(session, shared)
                return {'context': shared, 'level': 'shared' if shared else 'empty', 'others': others, 'notice': notice}
            if d.get('gate') and not m.needs_memory(d['query']):
                return {'context': '', 'level': 'skipped', 'others': others, 'notice': notice}
            budget = int(d.get('budget') or (self.cfg.prompt_tokens if d.get('fresh_only') else 6000))
            r = m.recall(d['query'], budget, session=session, live=self.live_sessions(space)).as_dict()
            if space != GLOBAL_SPACE:
                shared = self.shared_for(d['query'])
                if shared:
                    r['context'] = (r['context'] + '\n\n' + shared) if r.get('context') else shared
            if d.get('fresh_only'):                                    # v0.9.6: per-message memory: nothing given twice
                r['context'] = m.only_new(session, r['context'])
                r['tokens'] = len(r['context']) // 4
            return {**r, 'others': others, 'notice': notice}
        if path == '/wake':
            self.touch(space, d.get('session'))
            for s in self.idle_sessions(m, space):                     # v0.9: sessions that went quiet, not live parallel ones
                self.sleep_async(space, profile, s)
            return {'context': self.wake_context(m, space, d)}
        if path == '/clashcheck':                                      # v0.9.9: PostCompact hands over the summary
            args = (space, profile, d.get('session', 'default'), str(d.get('summary') or ''))
            if d.get('wait'):
                self.clash_check(*args)
            else:
                threading.Thread(target=self.clash_check, args=args, daemon=True).start()
            return {'started': True}
        if path == '/compacted':
            m.mark_compacted(d.get('session', 'default'))
            if d.get('wait'):                                          # v0.9.9: learn this chat BEFORE the summary replaces it
                try:
                    return {'slept': m.sleep(d.get('session'))}
                except Exception as ex:
                    print(f'[hypnos] sleep before compact failed for {space}: {ex}', flush=True)
                    return {'slept': None, 'error': str(ex)[:200]}
            return {'started': self.sleep_async(space, profile, d.get('session'))}
        if path == '/sleep':
            if d.get('wait'):
                return m.sleep(d.get('session'))
            return {'started': self.sleep_async(space, profile, d.get('session'))}
        if path == '/pin':
            if d.get('everywhere'):
                return {'id': self.mem(GLOBAL_SPACE).pin(d['claim'], d.get('session', 'default')), 'space': GLOBAL_SPACE}
            out = {'id': m.pin(d['claim'], d.get('session', 'default'))}
            self.changed(space)
            return out
        if path == '/review':
            return {'facts': m.review(d.get('topic'), int(d.get('limit', 200)))}
        if path == '/cleanup':
            return m.cleanup_suggestions(int(d.get('unused_days', 30)))
        if path == '/cleanup_view':
            return self.cleanup_view(space, m)
        if path == '/cleanup_apply':
            out = m.cleanup_apply(d.get('selection', ''), bool(d.get('hard')))
            if 'show' not in out:
                self.changed(space)
            return out
        if path == '/forget_ids':
            out = {'forgotten': m.forget_ids(list(d.get('ids') or []), bool(d.get('hard')))}
            self.changed(space)
            return out
        if path == '/purge':
            out = m.purge(bool(d.get('replaced')))
            self.changed(space)
            return out
        if path == '/pause':
            m.store.note('paused', '1' if d.get('paused', True) else '0')
            return {'paused': m.store.note('paused') == '1'}
        if path == '/correct':
            out = {'id': m.correct(d['fact_id'], d['claim'])}
            self.changed(space)
            return out
        if path == '/forget':
            out = {'forgotten': m.forget(d.get('fact_id'), d.get('query'), bool(d.get('hard')))}
            self.changed(space)
            return out
        if path == '/status':
            return {**m.status(), 'sleeping': space in self.sleeping}
        if path == '/export':
            return {'markdown': m.export()}
        if path == '/ping':
            return {'ok': True}
        raise KeyError(path)

    def idle_loop(self):
        while True:
            time.sleep(60)
            for space, m in list(self.mems.items()):              # v0.9: per session, so busy parallel sessions keep going
                for s in self.idle_sessions(m, space):
                    self.sleep_async(space, m.profile.name, s)


def stop_when_agents_close(svc):
    """v0.9: memory runs only while an agent is open. When the last one closes, stop the service and everything it
    started (writer models, laya-serve). Nothing is lost: every message is already on disk, and whatever was not
    sorted into facts yet is done the next time an agent opens."""
    import os
    import signal
    import subprocess
    while True:
        time.sleep(15)
        now = time.time()
        svc.agents = {a: t for a, t in svc.agents.items() if now - t < AGENT_TIMEOUT}
        if svc.agents or now - svc.last_activity < STOP_AFTER or now - svc.started < STOP_AFTER:
            continue
        print('[hypnos] no agent open: stopping', flush=True)
        if os.name == 'nt':                                         # this process and every child it started
            from .proc import hidden
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(os.getpid())], capture_output=True, **hidden())
        else:
            try:
                os.killpg(os.getpgid(0), signal.SIGTERM)
            except Exception:
                pass
        os._exit(0)


def keep_laya_running(cfg):
    """v0.9: with Laya as the System One model, start `laya-serve` on this machine (hidden) when it is not up."""
    import os
    import shutil
    import subprocess
    import urllib.request
    from .proc import background
    url = cfg.systemone_url or 'http://127.0.0.1:8000/v1/systemone'
    if '127.0.0.1' not in url and 'localhost' not in url:
        return
    exe = shutil.which('laya-serve')
    while exe:
        try:
            urllib.request.urlopen(url.rsplit('/v1/', 1)[0] + '/docs', timeout=3)
        except Exception:
            env = {**os.environ, 'LAYA_HOST': '127.0.0.1', 'LAYA_PORT': url.rsplit(':', 1)[1].split('/')[0],
                   'LAYA_DEFAULT_MODEL': os.environ.get('LAYA_DEFAULT_MODEL', 'english')}
            log = open(cfg.home / 'laya.log', 'a', encoding='utf-8')
            subprocess.Popen([exe], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, **background())
            time.sleep(120)                                          # first start downloads the model
        time.sleep(60)


def serve(host=None, port=None):
    if not isinstance(sys.stdout, _Stamped):                         # v0.9.9: dated log lines
        sys.stdout, sys.stderr = _Stamped(sys.stdout), _Stamped(sys.stderr)
    svc = Service()
    host, port = host or svc.cfg.host, port or svc.cfg.port

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                d = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                out, code = svc.handle(self.path, d), 200
            except KeyError as ex:
                out, code = {'error': f'unknown or missing: {ex}'}, 404
            except Exception as ex:
                out, code = {'error': str(ex)}, 500
            body = json.dumps(out, ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as ex:
                # v0.9.10: the caller (a hook with a short timeout) left before the answer: one dated line, no traceback
                print(f'[hypnos] {self.path}: caller closed the connection before the answer ({type(ex).__name__})', flush=True)

        def log_message(self, *a):
            pass

    threading.Thread(target=svc.idle_loop, daemon=True).start()
    if not svc.cfg.stay_running:
        threading.Thread(target=stop_when_agents_close, args=(svc,), daemon=True).start()
    if svc.cfg.systemone == 'laya' and svc.cfg.jev:
        threading.Thread(target=keep_laya_running, args=(svc.cfg,), daemon=True).start()
    print(f'[hypnos] serving on http://{host}:{port}  (memory in {svc.cfg.spaces_dir})', flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
