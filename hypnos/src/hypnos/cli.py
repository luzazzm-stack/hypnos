"""hypnos command line.

  hypnos serve                         run the local memory service (127.0.0.1:7439)
  hypnos mcp                           run the MCP server (stdio)
  hypnos hook claude <Event>           Claude Code hook handler (reads the event JSON on stdin)
  hypnos hook generic <event> [JSON]   any agent: start | prompt | tool | reply | compact | end
  hypnos chat-proxy --upstream URL     OpenAI-compatible proxy that gives any chat model memory
  hypnos setup [--yes] [--systemone jev|laya|none] [--uninstall]   connect every coding agent on this machine
  hypnos cleanup [--apply "1-5, T2"]   the /memory-cleanup list for this folder's memory (via the service)
  hypnos log|recall|sleep|pin|forget|status|export   work with a memory space by hand
"""
from __future__ import annotations
import argparse
import json
import sys

from .engine import Memory


def main(argv=None):
    ap = argparse.ArgumentParser(prog='hypnos', description='Layered agent memory that sleeps and shows its evidence.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('serve'); s.add_argument('--host'); s.add_argument('--port', type=int)
    sub.add_parser('mcp')
    h = sub.add_parser('hook'); h.add_argument('agent', choices=['claude', 'generic']); h.add_argument('event', nargs='?', default='')
    h.add_argument('payload', nargs='?', help='event JSON, if not given on stdin')
    cp = sub.add_parser('chat-proxy'); cp.add_argument('--upstream', required=True); cp.add_argument('--port', type=int, default=7440)
    cp.add_argument('--space', default='chat-default')
    for name in ('log', 'recall', 'sleep', 'pin', 'forget', 'status', 'export', 'wake'):
        p = sub.add_parser(name)
        p.add_argument('--space', default='default'); p.add_argument('--profile', default='generic')
        p.add_argument('--session', default='cli')
        if name in ('log', 'recall', 'pin', 'forget'):
            p.add_argument('text')
        if name == 'log':
            p.add_argument('--role', default='user')
        if name == 'forget':
            p.add_argument('--hard', action='store_true')
    c = sub.add_parser('cleanup'); c.add_argument('--apply', default=''); c.add_argument('--space'); c.add_argument('--hard', action='store_true')
    st = sub.add_parser('setup'); st.add_argument('--yes', action='store_true'); st.add_argument('--uninstall', action='store_true')
    st.add_argument('--systemone', choices=['jev', 'laya', 'none']); st.add_argument('--jev-key', default='')
    st.add_argument('--agents', default='', help='comma list: claude,opencode,codex,gemini,cursor,windsurf')
    st.add_argument('--memory-model', default='', help='comma list, best first, e.g. opencode:opencode/x-free,claude:haiku')
    st.add_argument('--list-models', action='store_true', help='print the models this machine can use for memory, ranked')
    a = ap.parse_args(argv)

    if a.cmd == 'serve':
        from .daemon import serve
        return serve(a.host, a.port)
    if a.cmd == 'mcp':
        from .mcp_server import main as mcp_main
        return mcp_main()
    if a.cmd == 'hook':
        from .hooks import claude, generic
        return (claude if a.agent == 'claude' else generic)(a.event, a.payload)
    if a.cmd == 'chat-proxy':
        from .integrations.chat_proxy import serve as proxy
        return proxy(a.upstream, a.port, a.space)

    if a.cmd == 'setup':
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        from .setup_wizard import main as setup_main
        return setup_main(a)
    if a.cmd == 'cleanup':                                # v0.9: fast, from the service's ready-made list
        from .client import call, current_space, ensure_running
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        ensure_running()
        base = {'space': a.space or current_space(), 'profile': 'coding'}
        if a.apply:
            r = call('/cleanup_apply', {**base, 'selection': a.apply, 'hard': a.hard}, timeout=120) or {'error': 'memory service not running'}
            print('\n'.join(r['show']) if 'show' in r else json.dumps(r))
        else:
            r = call('/cleanup_view', base, timeout=60) or {'error': 'memory service not running'}
            print(r.get('text') or r.get('error'))
        return 0

    m = Memory(a.space, a.profile)
    if a.cmd == 'log':
        print(m.log(a.text, a.role, a.session))
    elif a.cmd == 'recall':
        r = m.recall(a.text)
        print(r.context or '(nothing relevant)'); print(f'\n-- level={r.level} exact={r.exact} tokens={r.tokens}', file=sys.stderr)
    elif a.cmd == 'sleep':
        print(json.dumps(m.sleep(), indent=1))
    elif a.cmd == 'pin':
        print(m.pin(a.text, a.session))
    elif a.cmd == 'forget':
        print(m.forget(query=a.text, hard=a.hard))
    elif a.cmd == 'status':
        print(json.dumps(m.status(), indent=1))
    elif a.cmd == 'export':
        print(m.export())
    elif a.cmd == 'wake':
        print(m.wake() or '(memory is empty)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
