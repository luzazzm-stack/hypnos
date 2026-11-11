"""v0.9.8: `hypnos import`: start with what you already have instead of an empty memory.

  hypnos import ~/.claude/projects            past Claude Code chats (each goes to the memory of the folder it ran in)
  hypnos import notes.md docs/ --space S      text and Markdown files (one memory, --space or this folder's)

Messages go through the service like live ones (secrets removed, then sorted into facts when the memory sleeps), with
their real date, so older facts never override newer ones. Sorting uses the memory model, so a big import costs model
calls: run with --dry first to see how much there is. A file or chat imported once is skipped next time, and a chat
Hypnos already saw live (same session id) is skipped too.
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

from .client import call, ensure_running, space_for_dir
from .config import load_config

TEXT_EXT = {'.md', '.txt', '.markdown', '.rst'}
CHUNK = 3000
OWN = ('Long-term memory (hypnos', 'Relevant long-term memory (hypnos)', '[memory topics]', '<system-reminder>',
       '<command-name>', '<local-command-', 'All facts by topic, newest first')


def _done_file():
    return load_config().home / 'imported.json'


def _load_done() -> dict:
    try:
        return json.loads(_done_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save_done(done: dict):
    f = _done_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(done, indent=1), encoding='utf-8')


def _seen_live(space: str, session: str) -> bool:
    """Hypnos already logged this session while it ran: importing it again would only add duplicates."""
    db = load_config().db_path(space)
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        try:
            return con.execute('SELECT 1 FROM log WHERE session=? LIMIT 1', (session,)).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    return ''


def claude_chat(path: Path):
    """(session, cwd, [(role, text, ts)]) from one Claude Code transcript (.jsonl). Only what the user typed and what
    the agent answered: tool output, hidden context and Hypnos's own text are left out."""
    session, cwd, turns = path.stem, '', []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get('type') not in ('user', 'assistant') or j.get('isMeta') or j.get('isSidechain'):
            continue
        session, cwd = j.get('sessionId') or session, j.get('cwd') or cwd
        text = _text((j.get('message') or {}).get('content')).strip()
        if len(text) < 2 or any(m in text for m in OWN):
            continue
        turns.append(('user' if j['type'] == 'user' else 'agent', text[:8000], j.get('timestamp')))
    return session, cwd, turns


def text_file(path: Path):
    text = path.read_text(encoding='utf-8', errors='replace')
    parts, cur = [], ''
    for para in text.split('\n\n'):
        if cur and len(cur) + len(para) > CHUNK:
            parts.append(cur); cur = ''
        cur = (cur + '\n\n' + para) if cur else para
    if cur.strip():
        parts.append(cur)
    return parts


def plan(paths, space=None):
    """What would be imported: [(kind, file, space, session, items)]."""
    out = []
    for p in paths:
        p = Path(p).expanduser()
        files = sorted(p.rglob('*')) if p.is_dir() else [p]
        for f in files:
            if not f.is_file():
                continue
            if f.suffix == '.jsonl':
                session, cwd, turns = claude_chat(f)
                if turns:
                    sp = space or (space_for_dir(cwd) if cwd and Path(cwd).exists() else _space_from_path(cwd))
                    out.append(('chat', f, sp, session, turns))
            elif f.suffix.lower() in TEXT_EXT:
                parts = text_file(f)
                if parts:
                    out.append(('file', f, space or space_for_dir(str(Path.cwd())), 'import-' + f.stem[:40],
                                [('doc', t, None) for t in parts]))
    return out


def _space_from_path(cwd: str) -> str:
    """Space for a chat whose folder no longer exists: the folder's name, as the hooks would have named it."""
    name = Path(cwd.replace('\\', '/')).name if cwd else 'default'
    return f'code-{name or "default"}'


def run(paths, space=None, profile='coding', dry=False, limit=0) -> int:
    done = _load_done()
    todo, skipped = [], 0
    for kind, f, sp, session, items in plan(paths, space):
        key = str(f.resolve())
        if done.get(key) == f.stat().st_mtime or (kind == 'chat' and _seen_live(sp, session)):
            skipped += 1
            continue
        todo.append((kind, f, sp, session, items))
    todo.sort(key=lambda t: t[1].stat().st_mtime)                # oldest first, so newer facts replace older ones
    if limit:
        todo = todo[-limit:]                                    # --limit N: only the N newest
    n_items = sum(len(t[4]) for t in todo)
    chars = sum(len(x[1]) for t in todo for x in t[4])
    spaces = sorted({t[2] for t in todo})
    print(f'{len(todo)} to import ({n_items} messages or parts, about {chars // 4000}k tokens to sort), '
          f'{skipped} already in memory; memories: {", ".join(spaces) or "-"}')
    if dry or not todo:
        return 0
    if not ensure_running():
        print('hypnos: the memory service did not start')
        return 1
    for kind, f, sp, session, items in todo:
        for role, text, ts in items:
            meta = {'imported': str(f)}
            if ts:
                meta['ts'] = ts
            if kind == 'file':
                meta['evidence'] = [f'file:{f}']
            call('/log', {'space': sp, 'profile': profile, 'session': session, 'role': role, 'content': text, 'meta': meta},
                 timeout=30)
        call('/sleep', {'space': sp, 'profile': profile, 'session': session}, timeout=10)
        done[str(f.resolve())] = f.stat().st_mtime
        _save_done(done)
    print('imported; the memory sorts it into facts in the background (`hypnos status --space NAME` shows progress)')
    return 0
