"""Memory for normal chats: an OpenAI-compatible proxy.

Point any chat app or SDK at http://127.0.0.1:7440/v1 instead of the real API. For each request the proxy
logs the user's message, recalls relevant memory, adds it as a system message, forwards the request to the
upstream API (Ollama, OpenRouter, OpenAI, LM Studio ...) and logs the reply. Streaming is passed through.

    hypnos chat-proxy --upstream https://openrouter.ai/api/v1 --space chat-default
"""
from __future__ import annotations
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..client import call, ensure_running

MEMORY_NOTE = ('Long-term memory about this user from earlier conversations (hypnos). Use it when relevant; '
               'if it conflicts with what the user says now, the user wins.\n\n')


def _text(content):
    if isinstance(content, str):
        return content
    return ' '.join(p.get('text', '') for p in content or [] if isinstance(p, dict) and p.get('type') == 'text')


def serve(upstream: str, port: int = 7440, space: str = 'chat-default', host: str = '127.0.0.1'):
    upstream = upstream.rstrip('/')
    ensure_running()
    base = {'space': space, 'profile': 'chat'}

    class H(BaseHTTPRequestHandler):
        def _forward(self, method, body=None):
            path = self.path[3:] if self.path.startswith('/v1') else self.path
            headers = {k: v for k, v in self.headers.items() if k.lower() in ('authorization', 'content-type', 'accept', 'http-referer', 'x-title')}
            req = urllib.request.Request(upstream + path, data=body, method=method, headers=headers)
            return urllib.request.urlopen(req, timeout=600)

        def do_GET(self):
            try:
                with self._forward('GET') as r:
                    data = r.read()
                self.send_response(r.status); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(data)
            except Exception as ex:
                self.send_error(502, str(ex))

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            session = self.headers.get('X-Session-Id', 'chat')
            try:
                req = json.loads(raw or b'{}')
            except ValueError:
                req = {}
            msgs = req.get('messages') or []
            user = next((_text(m.get('content')) for m in reversed(msgs) if m.get('role') == 'user'), '')
            if self.path.endswith('/chat/completions') and user:
                call('/log', {**base, 'session': session, 'role': 'user', 'content': user}, timeout=3)
                mem = call('/recall', {**base, 'query': user, 'gate': True, 'budget': 2000}, timeout=15)
                if mem and mem.get('context'):
                    req['messages'] = [{'role': 'system', 'content': MEMORY_NOTE + mem['context']}] + msgs
                raw = json.dumps(req).encode()
            try:
                r = self._forward('POST', raw)
            except urllib.error.HTTPError as ex:
                self.send_response(ex.code); self.end_headers(); self.wfile.write(ex.read()); return
            except Exception as ex:
                self.send_error(502, str(ex)); return
            self.send_response(r.status)
            for k, v in r.headers.items():
                if k.lower() in ('content-type', 'cache-control'):
                    self.send_header(k, v)
            self.end_headers()
            reply = []
            if req.get('stream'):
                for line in r:                                   # pass SSE through, collect the text for memory
                    self.wfile.write(line); self.wfile.flush()
                    if line.startswith(b'data: ') and not line.startswith(b'data: [DONE]'):
                        try:
                            delta = json.loads(line[6:])['choices'][0].get('delta', {})
                            reply.append(delta.get('content') or '')
                        except (ValueError, KeyError, IndexError):
                            pass
            else:
                data = r.read(); self.wfile.write(data)
                try:
                    reply.append(json.loads(data)['choices'][0]['message']['content'] or '')
                except (ValueError, KeyError, IndexError):
                    pass
            text = ''.join(reply).strip()
            if text:
                threading.Thread(target=call, args=('/log', {**base, 'session': session, 'role': 'agent', 'content': text[:6000]}),
                                 kwargs={'timeout': 3}, daemon=True).start()

        def log_message(self, *a):
            pass

    print(f'[hypnos] chat proxy on http://{host}:{port}/v1 -> {upstream}  (memory space "{space}")', flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
