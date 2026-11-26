"""Writer LLMs used during sleep (extract facts, write topic summaries, wake note).
Everything that writes text goes here; every typed decision goes to Jev (jev.py)."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from .config import Config
from .proc import hidden


class Writer:
    name = 'base'

    def complete(self, prompt: str, attachments: list[str] = ()) -> str:
        raise NotImplementedError


def _kill_tree(proc):
    """Kill a process and its children (Windows shims like WinGet links start a second process)."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True, timeout=15, **hidden())
        else:
            proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


def _child_env():
    env = dict(os.environ)
    env['HYPNOS_DISABLE'] = '1'       # a writer that is itself an agent (claude, opencode) must not trigger our hooks
    return env


class OpencodeWriter(Writer):
    """Stock `opencode run` with an isolated, empty config (the free Zen tier only accepts genuine OpenCode)."""
    name = 'opencode'

    def __init__(self, cfg: Config, model: str | None = None, attempts: int = 5, timeout: int = 300):
        self.model = model or cfg.writer_model
        self.attempts = attempts
        self.timeout = timeout
        self.cfg_home = cfg.home / 'opencode-cfg'
        (self.cfg_home / 'opencode').mkdir(parents=True, exist_ok=True)
        conf = self.cfg_home / 'opencode' / 'opencode.jsonc'
        if not conf.exists():
            conf.write_text('{ "$schema": "https://opencode.ai/config.json" }\n', encoding='utf-8')
        self.run_dir = cfg.home / 'opencode-run'
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # prefer the real .exe: a .cmd/.bat wrapper would cut every argument at the first newline (the prompt!)
        self.exe = shutil.which('opencode.exe') or shutil.which('opencode') or 'opencode'

    def complete(self, prompt, attachments=()):
        files, tmp = [], []
        for a in attachments:
            f = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8', dir=self.run_dir)
            f.write(a); f.close(); tmp.append(f.name); files += ['-f', f.name]
        env = _child_env(); env['XDG_CONFIG_HOME'] = str(self.cfg_home)
        env['XDG_DATA_HOME'] = str(self.cfg_home / 'data')    # v0.9: its sessions stay out of the user's opencode history
        full = 'Do not use any tools. Answer directly from the attached file(s) and this message. ' + prompt
        err = 'timed out'                      # stays set when every attempt times out
        try:
            for attempt in range(self.attempts):
                proc = subprocess.Popen([self.exe, 'run', '-m', self.model, '--format', 'json', *files, '--', full],
                                        cwd=self.run_dir, env=env, stdin=subprocess.DEVNULL,   # no stdin: else EUNKNOWN read in background jobs
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', errors='replace', **hidden())
                try:
                    out, _ = proc.communicate(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)            # opencode (and its child process) would otherwise keep running and eat RAM
                    time.sleep(30); continue
                p = subprocess.CompletedProcess(proc.args, proc.returncode, out, '')
                texts, err = [], None
                for line in p.stdout.splitlines():
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get('type') == 'text':
                        texts.append(ev['part'].get('text', ''))
                    elif ev.get('type') == 'error':
                        err = ev.get('error')
                if texts and not err:
                    return '\n'.join(texts).strip()
                if attempt + 1 < self.attempts:
                    time.sleep(10 * (attempt + 1))
            raise RuntimeError(f'opencode writer failed ({self.model}): {str(err)[:200]}')
        finally:
            for t in tmp:
                try: os.unlink(t)
                except OSError: pass


class ClaudeWriter(Writer):
    """Claude Code in print mode (`claude -p`), e.g. with Haiku. Uses your Claude subscription."""
    name = 'claude'

    def __init__(self, cfg: Config, model: str | None = None):
        self.model = model or (cfg.writer_model if not cfg.writer_model.startswith('opencode/') else 'haiku')
        self.exe = shutil.which('claude') or 'claude'

    def complete(self, prompt, attachments=()):
        body = prompt + ''.join(f'\n\n--- attached file {i + 1} ---\n{a}' for i, a in enumerate(attachments))
        # A writer call is invisible to the user: no entry in the session list (--no-session-persistence) and none of
        # the user's hooks run for it (sounds, notifications, loggers), while the subscription login still works.
        p = subprocess.run([self.exe, '-p', '--model', self.model, '--output-format', 'json', '--strict-mcp-config',
                            '--no-session-persistence', '--settings', '{"disableAllHooks": true}'], input=body,
                           env=_child_env(), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300, **hidden())
        try:
            out = json.loads(p.stdout)
            if out.get('is_error') or not out.get('result'):
                raise RuntimeError('claude writer failed: ' + str(out.get('result'))[:200])
            return out['result'].strip()
        except ValueError:
            raise RuntimeError('claude writer failed: ' + p.stderr[-300:])


class CodexWriter(Writer):
    """OpenAI Codex CLI (`codex exec`), e.g. with the small GPT Luna model. Uses the user's ChatGPT plan."""
    name = 'codex'

    def __init__(self, cfg: Config, model: str | None = None):
        self.model = model or None                     # None: Codex's own default model
        self.exe = shutil.which('codex') or 'codex'

    def complete(self, prompt, attachments=()):
        body = prompt + ''.join(f'\n\n--- attached file {i + 1} ---\n{a}' for i, a in enumerate(attachments))
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'answer.txt'
            p = subprocess.run([self.exe, 'exec', '--skip-git-repo-check', *(['-m', self.model] if self.model else []),
                                '-o', str(out), '-'], input=body,
                               cwd=d, env=_child_env(), capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=300, **hidden())
            text = out.read_text(encoding='utf-8').strip() if out.exists() else ''
        if not text:
            raise RuntimeError('codex writer failed: ' + (p.stderr or p.stdout)[-300:])
        return text


def read_key(name: str, cfg: Config | None = None) -> str:
    """v0.9.10: an API key by its variable name, from the environment or else from the user's keys file
    (`keys_file` in config.toml, a KEY=value file). Keys are read when needed and never copied anywhere."""
    if not name:
        return ''
    if os.environ.get(name):
        return os.environ[name]
    path = getattr(cfg, 'keys_file', '') if cfg else ''
    if not path:
        return ''
    try:
        for line in Path(os.path.expanduser(str(path))).read_text(encoding='utf-8-sig').splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$', line)
            if m and m.group(1) == name and not line.lstrip().startswith('#'):
                return m.group(2).strip().strip('"\'')
    except OSError:
        pass
    return ''


class OpenAIWriter(Writer):
    """Any OpenAI-compatible chat API: Ollama, LM Studio, OpenRouter, OpenAI, Groq, Gemini, vLLM ..."""
    name = 'openai'

    def __init__(self, cfg: Config):
        self.url = cfg.openai_base_url.rstrip('/') + '/chat/completions'
        self.key = read_key(cfg.openai_api_key_env, cfg)
        self.model = cfg.writer_model
        self.timeout = 300

    def complete(self, prompt, attachments=()):
        body = prompt + ''.join(f'\n\n--- attached file {i + 1} ---\n{a}' for i, a in enumerate(attachments))
        req = urllib.request.Request(self.url, method='POST', data=json.dumps(
            {'model': self.model, 'messages': [{'role': 'user', 'content': body}], 'temperature': 0}).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': 'hypnos-memory/0.9', **({'Authorization': 'Bearer ' + self.key} if self.key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = json.loads(r.read())['choices'][0]['message'].get('content') or ''
        except urllib.error.HTTPError as ex:           # v0.9.10: keep the reason (429, quota, retry time) for the router
            try:
                detail = ex.read().decode('utf-8', 'replace')[:300]
            except Exception:
                detail = ''
            wait = ex.headers.get('Retry-After') if ex.headers else None
            raise RuntimeError(f'HTTP {ex.code}: {detail}' + (f' (retry after {wait}s)' if wait else '')) from None
        return re.sub(r'(?s)<think>.*?</think>', '', text).strip()   # reasoning models may put their thinking first


# v0.9.10: free OpenAI-compatible providers, written in the chain as `groq:MODEL`, `gemini:MODEL`, `openrouter:MODEL`
# (add `#KEY_NAME` to use another key, e.g. a second account). Keys come from the environment or `keys_file`.
PROVIDERS = {
    'groq': ('https://api.groq.com/openai/v1', 'GROQ_API_KEY'),
    'gemini': ('https://generativelanguage.googleapis.com/v1beta/openai', 'GEMINI_API_KEY'),
    'openrouter': ('https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY'),
}


class WriterOffline(RuntimeError):
    """No internet: the sleep is put off and retried later, instead of running through the whole chain."""
    fresh = False


class WritersResting(RuntimeError):
    """Every writer is resting after a usage limit: the sleep is put off until one is back."""


_LIMIT = re.compile(r"(?i)hit your .{0,20}limit|session limit|usage limit|weekly limit|rate.?limit|too many requests|"
                    r"\b429\b|quota|resource.?exhausted|credits?\b.{0,30}(exhaust|insufficient|run out)|out of credits|"
                    r"free usage limit|tokens per (minute|day)")
_OFFLINE = re.compile(r"(?i)can'?t reach the api server|ENOTFOUND|EAI_AGAIN|getaddrinfo failed|name or service not known|"
                      r"no address associated|network is unreachable|errno 1100[1-4]|temporary failure in name resolution|"
                      r"internet connection|ENETUNREACH")


def classify_error(text: str) -> str:
    """'offline', 'limit' (usage limit, quota, HTTP 429) or 'other'."""
    t = str(text or '')
    if _OFFLINE.search(t):
        return 'offline'
    if _LIMIT.search(t):
        return 'limit'
    return 'other'


def rest_seconds(text: str, spec: str, now_: float | None = None) -> float:
    """How long a writer rests after a limit error: the reset time in the message when there is one ("resets 6:45pm
    (Asia/Kathmandu)", "try again in 1m30s", "retry after 37s"); else 1 hour for Claude, 10 minutes for the others."""
    from datetime import datetime, timedelta
    t, now_ = str(text or ''), now_ or time.time()
    m = re.search(r'(?i)resets\s+(?:(?P<mon>[A-Z][a-z]{2,8})\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?'
                  r'(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>am|pm)(?:\s*\((?P<tz>[\w/+-]+)\))?', t)
    if m:
        tz = None
        if m.group('tz'):
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(m.group('tz'))
            except Exception:
                tz = None                           # no tz database: the laptop's own clock is the best guess
        cur = datetime.fromtimestamp(now_, tz) if tz else datetime.fromtimestamp(now_)
        h = int(m.group('h')) % 12 + (12 if m.group('ap').lower() == 'pm' else 0)
        at = cur.replace(hour=h, minute=int(m.group('mi') or 0), second=0, microsecond=0)
        if m.group('mon'):
            try:
                mon = datetime.strptime(m.group('mon')[:3], '%b').month
                at = at.replace(month=mon, day=int(m.group('day')))
                if at < cur - timedelta(days=1):
                    at = at.replace(year=at.year + 1)
            except ValueError:
                pass
        while at <= cur:
            at += timedelta(days=1)
        return min(8 * 86400, (at - cur).total_seconds() + 60)
    m = re.search(r'(?i)(?:try again|retry|retrydelay|retry after)[^0-9]{0,15}(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?!s))?\s*(?:([\d.]+)\s*s)?', t)
    if m and any(m.groups()):
        secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
        if secs > 0:
            return min(8 * 86400, max(5.0, secs + 2))
    return 3600.0 if spec.startswith('claude') else 600.0


class FakeWriter(Writer):
    """Deterministic, offline writer for tests and for running without any LLM.
    Extraction: every log line with a concrete value becomes a fact quoting itself. Summaries: first sentences of facts."""
    name = 'fake'

    def complete(self, prompt, attachments=()):
        src = attachments[0] if attachments else ''
        if 'JSON array of the JUNK numbers' in prompt:       # the keep-it-small check: junk = lines about a temp file
            return json.dumps([k + 1 for k, l in enumerate(src.splitlines()) if 'tmp_scratch' in l])
        if 'List the facts worth remembering' in prompt:
            out = []
            for line in src.splitlines():
                line = line.strip()
                body = re.sub(r'^\[[^\]]*\]\s*', '', line)
                if len(body) > 25 and (re.search(r'\d|[/\\_]|=|:|[a-z][A-Z]', body)
                                       or re.search(r'\b(prefer|favou?rite|switched|use)\b', body, re.I)):
                    kind = 'preference' if re.search(r'(?i)\b(prefer|favou?rite|switched|like)\b', body) else 'event'
                    ents = re.findall(r'\b[A-Z][a-z]+[A-Z]\w*|\b\w+_\w+|\b[A-Z][a-z]{3,}\b', body)[:5]
                    out.append({'claim': body[:200], 'kind': kind, 'quote': body[:180], 'entities': ents})
                if len(out) >= 12:
                    break
            return json.dumps(out)
        if 'Pick the ONE existing topic' in prompt:
            words = re.findall(r'[a-z]{4,}', src.lower())
            name = (words[0] if words else 'general')
            return json.dumps({'topic': name, 'new': True, 'desc': 'about ' + name})
        m = re.search(r'about (\d+) tokens', prompt)
        limit = int(m.group(1)) * 4 if m else 600
        lines = [re.sub(r'^\[[^\]]*\]\s*(\([^)]*\)\s*)?', '', l) for l in src.splitlines() if l.strip()]
        return ' '.join(lines)[:limit]


class ChainWriter(Writer):
    """v0.9: a list of writers in order of preference. A writer that fails rests for a while and the next one takes
    over, so sleep never depends on one model. Shared by every memory space in the process.
    v0.9.10 routing: a usage limit (Claude's 5-hour or session limit, HTTP 429, quota) rests that writer until the reset
    time in the message (else 1 h for Claude, 10 min for the others), saved in writer-rest.json so a restart keeps it;
    a Claude account limit rests every claude:* link. No internet stops the chain at once (WriterOffline) and the sleep
    is retried later. An answer that fails the caller's check goes to the next writer. Every call is tallied by writer
    so each sleep can log which writer did the work."""
    name = 'chain'
    COOLDOWN = 180                        # seconds a writer rests after any other failure (crash, empty answer, 5xx)
    OFFLINE_RETRY = 300                   # v0.9.10: after a no-internet error, sleeps wait this long before trying again
    down: dict = {}                       # link -> time it failed (short rest, this process only)
    offline_until = 0.0
    _lock = threading.Lock()

    def __init__(self, cfg: Config):
        self.links = []
        self.rest_file = Path(cfg.home) / 'writer-rest.json'
        self.tally: Counter = Counter()       # v0.9.10: successful calls per writer (read by Memory.sleep)
        for spec in cfg.writer_chain:
            kind, _, model = spec.partition(':')
            if kind in PROVIDERS:                       # groq:MODEL[#KEY_NAME] (v0.9.10)
                base, key_env = PROVIDERS[kind]
                model, _, other_key = model.partition('#')
                w = OpenAIWriter(cfg)
                w.url, w.model, w.timeout = base + '/chat/completions', model, 120
                w.key = read_key(other_key or key_env, cfg)
                if not w.key:
                    print(f'[hypnos] writer {spec} skipped: no {other_key or key_env} in the environment or keys_file', flush=True)
                    continue
                self.links.append((spec, w))
            elif kind == 'opencode':
                self.links.append((spec, OpencodeWriter(cfg, model, attempts=1, timeout=150)))   # in a chain: fail fast, the next model takes over
            elif kind == 'claude':
                self.links.append((spec, ClaudeWriter(cfg, model or 'haiku')))
            elif kind == 'codex':
                self.links.append((spec, CodexWriter(cfg, model or None)))
            elif kind == 'openai':                      # openai:MODEL[@BASE_URL][#KEY_ENV], e.g. DeepSeek or a local Ollama
                w = OpenAIWriter(cfg)
                model, _, key_env = model.partition('#')
                model, _, base = model.partition('@')
                w.model = model or w.model
                if base:
                    w.url = base.rstrip('/') + '/chat/completions'
                if key_env:
                    w.key = read_key(key_env, cfg)
                self.links.append((spec, w))
        self.last = None

    # ---- v0.9.10: rests after a usage limit, kept on disk
    @staticmethod
    def rest_key(spec, error=''):
        """A Claude limit is for the whole account (Haiku and Sonnet share it), unless it names one model."""
        if spec.startswith('claude') and not re.search(r'(?i)\b(opus|sonnet|haiku)\b', str(error)):
            return 'claude'
        return spec

    def rests(self) -> dict:
        try:
            data = json.loads(self.rest_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        now_ = time.time()
        return {k: v for k, v in data.items() if isinstance(v, dict) and float(v.get('until', 0)) > now_}

    def rest(self, key, seconds, reason=''):
        with self._lock:
            data = self.rests()
            data[key] = {'until': time.time() + seconds, 'reason': str(reason)[:160],
                         'set': time.strftime('%Y-%m-%d %H:%M:%S')}
            try:
                self.rest_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.rest_file.with_suffix('.tmp')
                tmp.write_text(json.dumps(data, indent=1), encoding='utf-8')
                os.replace(tmp, self.rest_file)
            except OSError:
                pass

    def resting(self, spec, rests=None) -> float:
        """Seconds until this link may be used again (0 = ready)."""
        rests = self.rests() if rests is None else rests
        until = max(float(rests.get(k, {}).get('until', 0)) for k in {spec, self.rest_key(spec)})
        return max(0.0, until - time.time())

    def complete(self, prompt, attachments=(), check=None):
        """`check(text) -> bool` is the caller's validation (e.g. "a JSON array of facts"). An answer that fails it goes
        to the next writer. If only the first writer of the chain answered and its answer failed the check, that answer
        is returned (the behaviour before v0.9.10); an invalid answer from a fallback writer is never used."""
        now_ = time.time()
        if now_ < ChainWriter.offline_until:
            raise WriterOffline('offline: writers are retried after ' + time.strftime('%H:%M', time.localtime(ChainWriter.offline_until)))
        rests = self.rests()
        usable = [l for l in self.links if not self.resting(l[0], rests)]
        if not usable:
            soon = min((self.resting(l[0], rests) for l in self.links), default=0)
            raise WritersResting('every writer is resting after a usage limit; the first is back at ' +
                                 time.strftime('%Y-%m-%d %H:%M', time.localtime(now_ + soon)))
        ready = [l for l in usable if now_ - self.down.get(l[0], 0) > self.COOLDOWN]
        errors, invalid = [], None
        for spec, w in ready or usable:               # only short rests left: try them anyway rather than fail
            if self.resting(spec, rests):             # put to rest earlier in this pass (Claude limit -> Sonnet too)
                continue
            try:
                out = w.complete(prompt, attachments)
                if not (out and out.strip()):
                    raise RuntimeError('empty answer')
            except Exception as ex:
                msg = str(ex)
                kind = classify_error(msg)
                if kind == 'offline':
                    ChainWriter.offline_until = time.time() + self.OFFLINE_RETRY
                    err = WriterOffline(f'offline ({spec}: {msg[:120]})')
                    err.fresh = True
                    print(f'[hypnos] writer {spec}: no internet, sleeps wait {self.OFFLINE_RETRY // 60} min: {msg[:120]}', flush=True)
                    raise err from None
                ChainWriter.down[spec] = time.time()
                if kind == 'limit':
                    secs = rest_seconds(msg, spec)
                    key = self.rest_key(spec, msg)
                    self.rest(key, secs, msg)
                    rests = self.rests()
                    print(f'[hypnos] writer {spec} hit a usage limit, {key} rests until '
                          f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + secs))}: {msg[:120]}', flush=True)
                else:
                    print(f'[hypnos] writer {spec} failed, trying the next one: {msg[:120]}', flush=True)
                errors.append(f'{spec}: {msg[:120]}')
                continue
            if check is not None:
                try:
                    ok = bool(check(out))
                except Exception:
                    ok = False
                if not ok:
                    print(f'[hypnos] writer {spec} gave an answer in the wrong format, trying the next one', flush=True)
                    errors.append(f'{spec}: wrong format')
                    if invalid is None and spec == self.links[0][0]:
                        invalid = (spec, out)
                    continue
            self.last = spec
            ChainWriter.down.pop(spec, None)
            with self._lock:
                self.tally[spec] += 1
            return out
        if invalid:
            self.last = invalid[0]
            with self._lock:
                self.tally[invalid[0]] += 1
            return invalid[1]
        raise RuntimeError('every writer failed: ' + ' | '.join(errors))


def write(writer: Writer, prompt: str, attachments=(), check=None) -> str:
    """v0.9.10: call a writer with an answer check; only the chain can use it to move on to the next writer."""
    if isinstance(writer, ChainWriter):
        return writer.complete(prompt, attachments, check=check)
    return writer.complete(prompt, attachments)


def is_json_list(text) -> bool:
    v = parse_json(text)
    return isinstance(v, list) and (not v or any(isinstance(x, dict) and x.get('claim') for x in v))


def is_json_object_with(key):
    return lambda text: isinstance(parse_json(text), dict) and bool(parse_json(text).get(key))


_NOT_TEXT = re.compile(r"(?i)^\s*(i'?m sorry|sorry,|i can(?:no|')t|i am unable|as an ai\b|error:)")


def is_prose(text) -> bool:
    """A summary or wake note: real text, not a refusal, an error or a JSON object."""
    t = (text or '').strip()
    return len(t) >= 15 and not _NOT_TEXT.search(t) and not t.startswith('{')


def make_writer(cfg: Config) -> Writer:
    if cfg.writer in ('auto', 'chain'):
        return ChainWriter(cfg)
    return {'opencode': OpencodeWriter, 'claude': ClaudeWriter, 'openai': OpenAIWriter, 'fake': lambda c: FakeWriter()}[cfg.writer](cfg)


def parse_json(text: str):
    t = re.sub(r'^```(?:json)?|```$', '', (text or '').strip(), flags=re.M).strip()
    for o, c in (('[', ']'), ('{', '}')):
        i, j = t.find(o), t.rfind(c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                pass
    return None
