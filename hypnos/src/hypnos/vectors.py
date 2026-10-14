"""Optional local meaning-based search (bge-small via fastembed, CPU). Loaded lazily; off by default to keep RAM low."""
from __future__ import annotations

QPREFIX = 'Represent this sentence for searching relevant passages: '
_SHARED = {}                                   # one embedding model per process, shared by every Memory (RAM)


class Vectors:
    def __init__(self, cfg, store):
        self.store = store
        self.enabled = cfg.vectors
        self._model = None
        self._np = None

    def _load(self):
        if self._model is None:
            try:
                import numpy as np
                from fastembed import TextEmbedding
            except ImportError:
                self.enabled = False
                return False
            self._np = np
            if 'model' not in _SHARED:
                _SHARED['model'] = TextEmbedding('BAAI/bge-small-en-v1.5', threads=2)
            self._model = _SHARED['model']
        return True

    def _embed(self, texts, query=False):
        np = self._np
        v = np.array(list(self._model.embed([(QPREFIX + t) if query else t[:1200] for t in texts], batch_size=8)), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def index(self, kind, items):
        """items: [(ref, text)]; only embeds refs not stored yet."""
        if not self.enabled or not items or not self._load():
            return
        have = {r['ref'] for r in self.store.q('SELECT ref FROM vectors WHERE kind=?', (kind,))}
        todo = [(ref, t) for ref, t in items if ref not in have]
        for i in range(0, len(todo), 32):
            chunk = todo[i:i + 32]
            vecs = self._embed([t for _, t in chunk])
            for (ref, _), v in zip(chunk, vecs):
                self.store.x('INSERT OR REPLACE INTO vectors(kind, ref, vec) VALUES (?,?,?)', (kind, ref, v.tobytes()))

    def rank(self, kind, query, refs):
        """Order the given refs by meaning-similarity to the query (refs without a vector go last)."""
        if not refs or not self.enabled or not self._load():
            return list(refs)
        marks = ','.join('?' * len(refs))
        rows = self.store.q(f'SELECT ref, vec FROM vectors WHERE kind=? AND ref IN ({marks})', [kind, *refs])
        if not rows:
            return list(refs)
        np = self._np
        mat = np.frombuffer(b''.join(r['vec'] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        sims = mat @ self._embed([query], query=True)[0]
        ordered = [rows[i]['ref'] for i in np.argsort(-sims)]
        return ordered + [r for r in refs if r not in set(ordered)]

    def search(self, kind, query, k=20):
        if not self.enabled or not self._load():
            return []
        rows = self.store.q('SELECT ref, vec FROM vectors WHERE kind=?', (kind,))
        if not rows:
            return []
        np = self._np
        mat = np.frombuffer(b''.join(r['vec'] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        sims = mat @ self._embed([query], query=True)[0]
        return [rows[i]['ref'] for i in np.argsort(-sims)[:k]]
