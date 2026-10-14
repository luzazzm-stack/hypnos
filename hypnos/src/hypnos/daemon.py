"""Local memory service: one process keeps every memory space (and the optional vector model) loaded,
so hooks and plugins stay fast. JSON over HTTP on 127.0.0.1 only. Also runs sleep when a space goes idle."""
from __future__ import annotations
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_config
from .engine import Memory

AGENT_TIMEOUT = 75             # v0.9: an open agent checks in every 30 s; silent this long = closed
STOP_AFTER = 90                # v0.9: no open agent and no activity this long -> the service stops
IDLE_SLEEP_MINUTES = 10        # sleep trigger: idle for this long with unconsolidated log (v0.9: was 30)
MAX_AWAKE_ROWS = 150           # sleep trigger: this many unconsolidated rows (v0.9: was 400)
GLOBAL_SPACE = 'global'        # v0.9: things the user wants remembered in every project


class Service:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
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
        threading.Thread(target=self._sleep_worker, daemon=True).start()
        threading.Thread(target=self._view_worker, daemon=True).start()

    def mem(self, space, profile='generic'):
        with self.lock:
            m = self.mems.get(space)
            if m is None or m.profile.name != profile and profile != 'generic':
                m = self.mems[space] = Memory(space, profile, cfg=self.cfg)
                m.fast_async = True               # logging never waits on the real-time fact path
                self.view_jobs.put(space)         # have the cleanup list ready before anyone asks
            return m

    def changed(self, space):
        """Memory content changed: rebuild the cleanup list in the background."""
        with self.lock:
            self.gen[space] = self.gen.get(space, 0) + 1
        self.view_jobs.put(space)

    def _view_worker(self):
        for db in sorted(self.cfg.spaces_dir.glob('*.db')):        # warm every existing memory at start-up
            try:
                self.mem(db.stem, 'coding' if db.stem.startswith('code-') else 'generic')
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

    def sleep_async(self, space, profile, session=None):
        """Queue a sleep. Many sessions (Claude Code, opencode, other agents) can ask at once; the queue runs them one
        after another so the writer models are never hit in parallel, and the same request is never queued twice."""
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
            try:
                self.mem(space, profile).sleep(session)
            except Exception as ex:                      # a failed sleep leaves the watermark; next sleep retries
                print(f'[hypnos] sleep failed for {space}: {ex}', flush=True)
            finally:
                self.changed(space)
                self.sleeping.discard(space)
                with self.lock:
                    self.queued.discard((space, session))

    def touch(self, space, session):
        if session:
            self.seen[(space, session)] = time.time()

    def live_sessions(self, space, minutes=IDLE_SLEEP_MINUTES):
        """Sessions of this memory space active in the last few minutes (Claude Code, opencode, other agents)."""
        cutoff = time.time() - minutes * 60
        return {s for (sp, s), t in self.seen.items() if sp == space and t > cutoff}

    def others_now(self, m, space, session):
        """v0.9: the other sessions working in this project right now, each with what its user asked last."""
        out = []
        for s in sorted(self.live_sessions(space) - {session}):
            task = m.store.note('task:' + s)
            if task:
                out.append(f'- another session: "{task[:160]}"')
        return out

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
        space, profile = d.get('space', 'default'), d.get('profile', 'generic')
        m = self.mem(space, profile)
        if path == '/log':
            if m.store.note('paused') == '1':                         # v0.9: the user paused memory for this space
                return {'id': None, 'paused': True}
            lid = m.log(d['content'], d.get('role', 'user'), d.get('session', 'default'), d.get('meta'))
            self.last_log[space] = time.time()
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
            if not m.has_memory_beyond(session):                       # v0.9: nothing outside this conversation yet
                return {'context': '', 'level': 'empty', 'others': others}
            if d.get('gate') and not m.needs_memory(d['query']):
                return {'context': '', 'level': 'skipped', 'others': others}
            r = m.recall(d['query'], int(d.get('budget', 6000)), session=session, live=self.live_sessions(space)).as_dict()
            return {**r, 'others': others}
        if path == '/wake':
            self.touch(space, d.get('session'))
            for s in self.idle_sessions(m, space):                     # v0.9: sessions that went quiet, not live parallel ones
                self.sleep_async(space, profile, s)
            ctx = m.wake(d.get('session'))
            if space != GLOBAL_SPACE:
                pins = self.mem(GLOBAL_SPACE).store.q("SELECT claim FROM facts WHERE source='user' AND status='active' ORDER BY rowid DESC LIMIT 15")
                if pins:
                    block = '[things the user told me to remember in every project]\n' + '\n'.join('- ' + p['claim'] for p in pins)
                    ctx = (ctx + '\n\n' + block) if ctx else block
            return {'context': ctx}
        if path == '/compacted':
            m.mark_compacted(d.get('session', 'default'))
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
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    threading.Thread(target=svc.idle_loop, daemon=True).start()
    if not svc.cfg.stay_running:
        threading.Thread(target=stop_when_agents_close, args=(svc,), daemon=True).start()
    if svc.cfg.systemone == 'laya' and svc.cfg.jev:
        threading.Thread(target=keep_laya_running, args=(svc.cfg,), daemon=True).start()
    print(f'[hypnos] serving on http://{host}:{port}  (memory in {svc.cfg.spaces_dir})', flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
