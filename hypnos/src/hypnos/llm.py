"""Writer LLMs used during sleep (extract facts, write topic summaries, wake note).
Everything that writes text goes here; every typed decision goes to Jev (jev.py)."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
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
        # --no-session-persistence: writer calls must not show up in the user's Claude Code session list
        p = subprocess.run([self.exe, '-p', '--model', self.model, '--output-format', 'json', '--strict-mcp-config',
                            '--no-session-persistence'], input=body,
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


class OpenAIWriter(Writer):
    """Any OpenAI-compatible chat API: Ollama, LM Studio, OpenRouter, OpenAI, vLLM ..."""
    name = 'openai'

    def __init__(self, cfg: Config):
        self.url = cfg.openai_base_url.rstrip('/') + '/chat/completions'
        self.key = os.environ.get(cfg.openai_api_key_env, '')
        self.model = cfg.writer_model

    def complete(self, prompt, attachments=()):
        body = prompt + ''.join(f'\n\n--- attached file {i + 1} ---\n{a}' for i, a in enumerate(attachments))
        req = urllib.request.Request(self.url, method='POST', data=json.dumps(
            {'model': self.model, 'messages': [{'role': 'user', 'content': body}], 'temperature': 0}).encode(),
            headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + self.key} if self.key else {})})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())['choices'][0]['message']['content'].strip()


class FakeWriter(Writer):
    """Deterministic, offline writer for tests and for running without any LLM.
    Extraction: every log line with a concrete value becomes a fact quoting itself. Summaries: first sentences of facts."""
    name = 'fake'

    def complete(self, prompt, attachments=()):
        src = attachments[0] if attachments else ''
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
    """v0.9: a list of writers, cheapest first. A writer that fails (rate limit, quota, outage) rests for a while and the
    next one takes over, so sleep never depends on one model. Bigger, paid models come last, used only if every
    cheaper one is down. Shared by every memory space in the process."""
    name = 'chain'
    COOLDOWN = 1800                       # seconds a failed writer rests before it is tried again
    down: dict = {}                       # link -> time it failed

    def __init__(self, cfg: Config):
        self.links = []
        for spec in cfg.writer_chain:
            kind, _, model = spec.partition(':')
            if kind == 'opencode':
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
                    w.key = os.environ.get(key_env, '')
                self.links.append((spec, w))
        self.last = None

    def complete(self, prompt, attachments=()):
        now_ = time.time()
        ready = [l for l in self.links if now_ - self.down.get(l[0], 0) > self.COOLDOWN]
        errors = []
        for spec, w in ready or self.links:           # all resting: try them anyway rather than fail
            try:
                out = w.complete(prompt, attachments)
                if out and out.strip():
                    self.last = spec
                    ChainWriter.down.pop(spec, None)
                    return out
                raise RuntimeError('empty answer')
            except Exception as ex:
                ChainWriter.down[spec] = time.time()
                errors.append(f'{spec}: {str(ex)[:120]}')
                print(f'[hypnos] writer {spec} failed, trying the next one: {str(ex)[:120]}', flush=True)
        raise RuntimeError('every writer failed: ' + ' | '.join(errors))


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
