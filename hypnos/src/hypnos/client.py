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


def space_for_dir(cwd: str, prefix='code') -> str:
    """Coding memory space = the git repository (or folder) the agent is working in."""
    try:
        top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd, capture_output=True, text=True, timeout=3, **hidden()).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        top = ''
    return f'{prefix}-{Path(top or cwd).name}'
