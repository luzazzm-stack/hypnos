"""MCP server (stdio) exposing memory tools to any MCP client: Claude Code, opencode, Claude Desktop, Cursor, ...
The memory space defaults to HYPNOS_SPACE, else the git repo of the current directory."""
from __future__ import annotations
import json
import os

from .client import call, ensure_running, space_for_dir


def _check_in_while_open():
    """v0.9: this MCP server lives exactly as long as the agent that started it. It tells the memory service it is
    open every 30 s (restarting the service if needed) and says goodbye on exit, so the service can stop when the
    last agent closes."""
    import atexit
    import threading
    import time
    import uuid
    agent = f'{os.getpid()}-{uuid.uuid4().hex[:6]}'

    def beat():
        while True:
            if call('/attach', {'agent': agent}, timeout=3) is None:
                ensure_running()
                call('/attach', {'agent': agent}, timeout=3)
            time.sleep(30)
    threading.Thread(target=beat, daemon=True).start()
    atexit.register(lambda: call('/detach', {'agent': agent}, timeout=2))


def main():
    try:                                                   # mcp 2.x renamed FastMCP to MCPServer
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as ex:
            raise SystemExit(f'The MCP server needs the "mcp" package ({ex}):  pip install "hypnos[mcp]"')

    space = os.environ.get('HYPNOS_SPACE') or space_for_dir(os.getcwd())
    profile = os.environ.get('HYPNOS_PROFILE', 'coding')
    base = {'space': space, 'profile': profile, 'session': os.environ.get('HYPNOS_SESSION', 'mcp')}
    ensure_running()
    _check_in_while_open()
    app = FastMCP('hypnos')

    def _call(path, extra, timeout=30):
        r = call(path, {**base, **extra}, timeout=timeout)
        return r if r is not None else {'error': 'hypnos service is not running; start it with `hypnos serve`'}

    @app.tool()
    def memory_recall(query: str) -> str:
        """Search long-term memory of earlier sessions. Returns summaries for orientation plus verified facts with the exact
        log quotes they came from. For exact values (paths, IDs, settings, commands) rely on the quoted evidence."""
        r = _call('/recall', {'query': query, 'budget': 4000})
        return r.get('context') or r.get('error') or 'Nothing relevant in memory.'

    @app.tool()
    def memory_remember(fact: str, everywhere: bool = False) -> str:
        """Store something the user explicitly wants remembered (a preference, decision or fact). Highest trust after project rules.
        everywhere=True keeps it for every project, not just this one."""
        return json.dumps(_call('/pin', {'claim': fact, 'everywhere': everywhere}))

    @app.tool()
    def memory_review(topic: str = '', limit: int = 100) -> str:
        """List what memory holds for this project (newest first), with fact ids, topics and how often each was used."""
        return json.dumps(_call('/review', {'topic': topic or None, 'limit': limit}), ensure_ascii=False)

    @app.tool()
    def memory_cleanup_suggestions(unused_days: int = 30) -> str:
        """Suggest what to delete: near-duplicate facts, facts never used in `unused_days` days, stale facts, and how much
        archived/replaced history a purge would remove. Deletes nothing; show the list to the user and let them choose."""
        return json.dumps(_call('/cleanup', {'unused_days': unused_days}), ensure_ascii=False)

    @app.tool()
    def memory_cleanup_view() -> str:
        """The /memory-cleanup list, ready to show: numbered suggestions (duplicates, unused, stale) and topics T1, T2...
        with counts. Show it to the user exactly as returned. Deletes nothing."""
        r = _call('/cleanup_view', {})
        return r.get('text') or r.get('error') or 'Memory is empty.'

    @app.tool()
    def memory_cleanup_apply(selection: str, delete_source: bool = False) -> str:
        """Delete what the user picked from the memory_cleanup_view list, in their words: "1-5, 9", "T2", "all duplicates",
        "all unused", or "show T3" to list a topic's facts. Add "purge" to delete forgotten facts for good. Only after the
        user chose."""
        return json.dumps(_call('/cleanup_apply', {'selection': selection, 'hard': delete_source}, timeout=120), ensure_ascii=False)

    @app.tool()
    def memory_forget_ids(ids: list[str], delete_source: bool = False) -> str:
        """Forget exactly these fact ids (from memory_review or memory_cleanup_suggestions). Only after the user agreed."""
        return json.dumps(_call('/forget_ids', {'ids': ids, 'hard': delete_source}))

    @app.tool()
    def memory_purge(include_replaced: bool = False) -> str:
        """Permanently delete forgotten facts (and old replaced versions if include_replaced) and shrink the memory file.
        Cannot be undone; only after the user agreed."""
        return json.dumps(_call('/purge', {'replaced': include_replaced}, timeout=120))

    @app.tool()
    def memory_pause(paused: bool = True) -> str:
        """Stop (or restart) saving new things to memory for this project, e.g. while handling something private."""
        return json.dumps(_call('/pause', {'paused': paused}))

    @app.tool()
    def memory_correct(fact_id: str, corrected: str) -> str:
        """Replace a remembered fact that the user says is wrong. The old fact is kept as superseded."""
        return json.dumps(_call('/correct', {'fact_id': fact_id, 'claim': corrected}))

    @app.tool()
    def memory_forget(query: str, delete_source: bool = False) -> str:
        """Forget facts matching the query. delete_source=True also deletes the original log text (privacy)."""
        return json.dumps(_call('/forget', {'query': query, 'hard': delete_source}))

    @app.tool()
    def memory_note(note: str) -> str:
        """Write a short note into memory: what was done, decided, learned or left open. For agents without automatic
        memory hooks, call it at the end of each task; it becomes facts at the next sleep."""
        return json.dumps(_call('/log', {'role': 'agent', 'content': note}))

    @app.tool()
    def memory_sleep() -> str:
        """Consolidate recent activity into verified facts and topic summaries now (normally automatic)."""
        return json.dumps(_call('/sleep', {}))

    @app.tool()
    def memory_status() -> str:
        """Show how much is in memory for this space."""
        return json.dumps(_call('/status', {}))

    app.run()
