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

from .client import call, ensure_running, session_space, space_for_dir
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


def _count(space: str, sql: str, args: tuple) -> int:
    db = load_config().db_path(space)
    if not db.exists():
        return 0
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        try:
            return con.execute(sql, args).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def _seen_live(space: str, session: str) -> bool:
    """Hypnos already logged this session while it ran: importing it again would only add duplicates.
    Rows written by an import (meta.imported) do not count: those are not a live session."""
    return _count(space, 'SELECT COUNT(*) FROM log WHERE session=? AND instr(meta, ?) = 0', (session, '"imported"')) > 0


def _imported_count(space: str, session: str, f: Path) -> int:
    """How many messages of this file an earlier (cut short) import already stored. The import sends messages in
    order and stops at the first failure, so these are always the first N: a rerun continues after them."""
    mark = json.dumps({'imported': str(f)})[1:-1]                # '"imported": "<path>"', as stored in meta
    return _count(space, 'SELECT COUNT(*) FROM log WHERE session=? AND instr(meta, ?) > 0', (session, mark))


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
                    sp = space or _space_for_chat(session, cwd)
                    out.append(('chat', f, sp, session, turns))
            elif f.suffix.lower() in TEXT_EXT:
                parts = text_file(f)
                if parts:
                    out.append(('file', f, space or space_for_dir(str(Path.cwd())), 'import-' + f.stem[:40],
                                [('doc', t, None) for t in parts]))
    return out


def _space_for_chat(session: str, cwd: str) -> str:
    """The memory a chat belongs to, named as the hooks name it: the space pinned to the session when it ran (if still
    recorded), else the git repository of its folder, else (folder gone) the repository found by walking up the
    recorded path, else the folder's own name."""
    pinned = session_space(session)
    if pinned:
        return pinned
    if cwd and Path(cwd).exists():
        return space_for_dir(cwd)
    return _space_from_path(cwd)


def _space_from_path(cwd: str) -> str:
    """Space for a chat whose folder no longer exists. A chat run in a subfolder of a repository belongs to the
    repository's memory (as with space_for_dir), so walk up the recorded path to the nearest folder that has a .git;
    without one, the folder's own name."""
    if not cwd:
        return 'code-default'
    p = Path(cwd.replace('\\', '/'))
    for up in (p, *p.parents):
        if (up / '.git').exists():
            return f'code-{up.name}'
    return f'code-{p.name or "default"}'


def run(paths, space=None, profile='coding', dry=False, limit=0) -> int:
    done = _load_done()
    todo, skipped = [], 0
    for kind, f, sp, session, items in plan(paths, space):
        key = str(f.resolve())
        if done.get(key) == f.stat().st_mtime or (kind == 'chat' and _seen_live(sp, session)):
            skipped += 1
            continue
        items = items[_imported_count(sp, session, f):]           # a cut-short or continued chat: only what is not stored yet
        if not items:
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
    failed = 0
    for kind, f, sp, session, items in todo:
        sent = 0
        for role, text, ts in items:
            meta = {'imported': str(f)}
            if ts:
                meta['ts'] = ts
            if kind == 'file':
                meta['evidence'] = [f'file:{f}']
            r = call('/log', {'space': sp, 'profile': profile, 'session': session, 'role': role, 'content': text, 'meta': meta},
                     timeout=30)
            if not isinstance(r, dict) or r.get('id') is None:   # service down, error, or the memory is paused
                break                                           # stop here so what is stored stays the first N messages
            sent += 1
        if sent:
            call('/sleep', {'space': sp, 'profile': profile, 'session': session}, timeout=10)
        if sent < len(items):
            failed += len(items) - sent
            print(f'hypnos: {f}: {len(items) - sent} of {len(items)} messages were not stored '
                  f'(service error or memory paused); not marked as imported, run import again to continue')
            continue
        done[str(f.resolve())] = f.stat().st_mtime
        _save_done(done)
    if failed:
        print(f'{failed} messages failed; the rest was imported')
        return 1
    print('imported; the memory sorts it into facts in the background (`hypnos status --space NAME` shows progress)')
    return 0
