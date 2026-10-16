"""Tiny client for the local service. Starts it in the background when it is not running.
If the service cannot be reached, callers get None and must carry on: memory must never break an agent."""
from __future__ import annotations
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import load_config
from .proc import background, hidden, windowless_python


def _url(path):
    cfg = load_config()
    return f'http://{cfg.host}:{cfg.port}{path}'


def call(path, payload=None, timeout=10.0):
    req = urllib.request.Request(_url(path), data=json.dumps(payload or {}).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def service_command() -> list:
    """The Python engine (the main one). HYPNOS_ENGINE=rust uses the frozen Rust engine in future/agentmem-rs instead."""
    import shutil
    rust = os.environ.get('AGENTMEM_RUST_EXE') or shutil.which('agentmem-rs')
    if rust and os.environ.get('HYPNOS_ENGINE', 'python') == 'rust':
        return [rust, 'serve']
    return [windowless_python(), '-m', 'hypnos', 'serve']      # v0.9: pythonw on Windows, so no window


def ensure_running(wait=6.0) -> bool:
    if call('/ping', timeout=1.0):
        return True
    cfg = load_config()
    log = open(cfg.home / 'daemon.log', 'a', encoding='utf-8')
    subprocess.Popen(service_command(), stdout=log, stderr=log, stdin=subprocess.DEVNULL, **background())
    t0 = time.time()
    while time.time() - t0 < wait:
        if call('/ping', timeout=0.5):
            return True
        time.sleep(0.25)
    return False


def _pin_file(session: str) -> Path | None:
    safe = ''.join(ch for ch in str(session or '') if ch.isalnum() or ch in '-_')[:80]
    if not safe or safe in ('default', 'mcp'):
        return None
    return load_config().home / 'session-spaces' / f'{safe}.txt'


def session_space(session: str, cwd: str | None = None, prefix='code') -> str | None:
    """The memory space a session belongs to: the folder it STARTED in. A session that later moves to another folder
    keeps its one memory, so logging, recall and /memory-cleanup always use the same space.
    With `cwd`, pins the session on first sight; without it, only looks the pin up (None if the session is unknown)."""
    f = _pin_file(session)
    try:
        if f and f.exists():
            return f.read_text(encoding='utf-8').strip() or None
    except OSError:
        pass
    if cwd is None:
        return None
    space = space_for_dir(cwd, prefix)
    if f:
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(space, encoding='utf-8')
            cutoff = time.time() - 60 * 86400              # forget pins of sessions older than 60 days
            for old in f.parent.glob('*.txt'):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
        except OSError:
            pass
    return space


def current_space(cwd: str | None = None) -> str:
    """Space for a command run inside an agent session (CLI, MCP server): HYPNOS_SPACE, else the space the running
    Claude Code session is pinned to, else the folder."""
    return (os.environ.get('HYPNOS_SPACE') or session_space(os.environ.get('CLAUDE_CODE_SESSION_ID', ''))
            or space_for_dir(cwd or os.getcwd()))


def space_for_dir(cwd: str, prefix='code') -> str:
    """Coding memory space = the git repository (or folder) the agent is working in."""
    try:
        top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd, capture_output=True, text=True, timeout=3, **hidden()).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        top = ''
    return f'{prefix}-{Path(top or cwd).name}'
