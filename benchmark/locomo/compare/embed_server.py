"""Tiny OpenAI-compatible embedding server (llama-server style) backed by fastembed bge-small.
Lets GBrain use the same local embedding model as agentmem, with no API keys.
    uv run --no-project --python 3.12 --with fastembed python embed_server.py   (listens on 127.0.0.1:8080)
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

from fastembed import TextEmbedding

MODEL = 'BAAI/bge-small-en-v1.5'
emb = TextEmbedding(MODEL, threads=2)
lock = Lock()


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip('/').endswith('models'):
            return self._send(200, {'object': 'list', 'data': [{'id': 'bge-small', 'object': 'model'}]})
        self._send(200, {'status': 'ok'})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        inp = req.get('input', [])
        inp = [inp] if isinstance(inp, str) else inp
        with lock:
            vecs = [v.tolist() for v in emb.embed(inp, batch_size=8)]
        self._send(200, {'object': 'list', 'model': req.get('model', 'bge-small'),
                         'data': [{'object': 'embedding', 'index': i, 'embedding': v} for i, v in enumerate(vecs)],
                         'usage': {'prompt_tokens': 0, 'total_tokens': 0}})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 8080), H).serve_forever()
