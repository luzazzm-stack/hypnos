"""SQLite storage for one memory space: raw log, episodes, facts, topics, links, decisions. FTS5 for keyword search."""
from __future__ import annotations
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts TEXT, role TEXT, content TEXT,
  meta TEXT DEFAULT '{}', redacted TEXT DEFAULT '[]', tombstone TEXT);
CREATE TABLE IF NOT EXISTS watermark(session TEXT PRIMARY KEY, upto INTEGER);
CREATE TABLE IF NOT EXISTS episodes(
  id TEXT PRIMARY KEY, session TEXT, start_id INTEGER, end_id INTEGER, topic TEXT,
  claims_extracted INTEGER, claims_rejected INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, episode TEXT, claim TEXT, kind TEXT, source TEXT, quote TEXT,
  evidence TEXT DEFAULT '[]', evidence_status TEXT DEFAULT 'ok', valid_from TEXT, valid_to TEXT,
  superseded_by TEXT, scope TEXT DEFAULT '{}', importance INTEGER DEFAULT 3, recalled INTEGER DEFAULT 0,
  useful INTEGER DEFAULT 0, last_used TEXT, status TEXT DEFAULT 'active', verify_p REAL, meta TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS topics(
  id TEXT PRIMARY KEY, name TEXT, desc TEXT, small TEXT DEFAULT '', medium TEXT DEFAULT '', big TEXT DEFAULT '',
  anchors TEXT DEFAULT '[]', dirty INTEGER DEFAULT 1, updated TEXT, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS links(src TEXT, dst TEXT, type TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, question TEXT,
  answer TEXT, p REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS notes(key TEXT PRIMARY KEY, value TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS vectors(kind TEXT, ref TEXT, vec BLOB, PRIMARY KEY(kind, ref));
CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, name TEXT, type TEXT DEFAULT 'thing', aliases TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS fact_entities(fid TEXT, eid TEXT, PRIMARY KEY(fid, eid));
CREATE INDEX IF NOT EXISTS fe_eid ON fact_entities(eid);
CREATE INDEX IF NOT EXISTS facts_sup ON facts(superseded_by);
CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(content, content='log', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(fid UNINDEXED, text);
CREATE VIRTUAL TABLE IF NOT EXISTS topics_fts USING fts5(tid UNINDEXED, level UNINDEXED, text);
CREATE TRIGGER IF NOT EXISTS log_ai AFTER INSERT ON log BEGIN
  INSERT INTO log_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TRIGGER IF NOT EXISTS log_au AFTER UPDATE OF content ON log BEGIN
  INSERT INTO log_fts(log_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO log_fts(rowid, content) VALUES (new.id, new.content); END;
"""

_WORD = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.\-/\\]*')
STOP = set('a an the of to in on for and or is are was were be been what which who whom whose when where why how did do does '
           'with by at from that this these those it its as into about my your our their was i you we they me us them '
           'there here then than so if not no yes can could would should will shall may might must have has had'.split())


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + 'Z'


def entity_id(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', (name or '').lower()).strip('_')[:60]


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of its content words."""
    words = []
    for w in _WORD.findall(text.lower()):
        for part in re.split(r'[.\-/\\]', w):
            if len(part) > 1 and part not in STOP:
                words.append(part)
    words = list(dict.fromkeys(words))[:24]
    return ' OR '.join(f'"{w}"' for w in words)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()     # R14: one writer at a time
        with self.lock:
            self.db.executescript(SCHEMA)

    # ---------- generic ----------
    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def batch(self, statements):
        """v0.9: many writes in ONE transaction (a commit per row made deleting a topic slow)."""
        with self.lock:
            for sql, args in statements:
                self.db.execute(sql, args)
            self.db.commit()

    # ---------- log ----------
    def append(self, session, role, content, meta=None, redacted=()):
        ts = (meta or {}).get('ts') or now()          # event time: callers may pass the real time (e.g. imported chats)
        cur = self.x('INSERT INTO log(session, ts, role, content, meta, redacted) VALUES (?,?,?,?,?,?)',
                     (session, ts, role, content, json.dumps(meta or {}), json.dumps(list(redacted))))
        return cur.lastrowid

    def unconsolidated(self, session=None):
        sql = ('SELECT l.* FROM log l LEFT JOIN watermark w ON w.session = l.session '
               'WHERE l.id > COALESCE(w.upto, 0) AND l.tombstone IS NULL')
        args = ()
        if session:
            sql += ' AND l.session = ?'; args = (session,)
        return self.q(sql + ' ORDER BY l.id', args)

    def set_watermark(self, session, upto):
        self.x('INSERT INTO watermark(session, upto) VALUES (?,?) ON CONFLICT(session) DO UPDATE SET upto=excluded.upto',
               (session, upto))

    def log_range(self, start_id, end_id):
        return self.q('SELECT * FROM log WHERE id BETWEEN ? AND ? AND tombstone IS NULL ORDER BY id', (start_id, end_id))

    def search_log(self, text, k=10, id_ranges=None):
        fq = fts_query(text)
        if not fq:
            return []
        rows = self.q('SELECT l.*, bm25(log_fts) AS score FROM log_fts JOIN log l ON l.id = log_fts.rowid '
                      'WHERE log_fts MATCH ? AND l.tombstone IS NULL ORDER BY score LIMIT ?', (fq, k * 8 if id_ranges else k))
        if id_ranges:
            rows = [r for r in rows if any(a <= r['id'] <= b for a, b in id_ranges)][:k]
        return rows

    def tombstone(self, log_id, reason):                      # R1 / R5 privacy path
        self.x("UPDATE log SET content='', tombstone=? WHERE id=?", (json.dumps({'deleted': now(), 'reason': reason}), log_id))
        self.x("UPDATE facts SET evidence_status='deleted' WHERE evidence LIKE ?", (f'%log:{log_id}%',))

    # ---------- facts ----------
    def add_fact(self, f: dict):
        cols = ['id', 'episode', 'claim', 'kind', 'source', 'quote', 'evidence', 'valid_from', 'scope', 'importance', 'verify_p', 'meta']
        vals = [f.get('id'), f.get('episode'), f['claim'], f.get('kind', 'event'), f.get('source', 'observed'), f.get('quote', ''),
                json.dumps(f.get('evidence', [])), f.get('valid_from', now()), json.dumps(f.get('scope', {})),
                int(f.get('importance', 3)), f.get('verify_p'), json.dumps(f.get('meta', {}))]
        self.x(f'INSERT OR REPLACE INTO facts({",".join(cols)}) VALUES ({",".join("?" * len(cols))})', vals)
        self.x('DELETE FROM facts_fts WHERE fid=?', (f['id'],))
        self.x('INSERT INTO facts_fts(fid, text) VALUES (?,?)',
               (f['id'], f['claim'] + ' ' + f.get('quote', '') + ' ' + ' '.join(f.get('entities', []))))
        for name in f.get('entities', []):
            eid = entity_id(name)
            if eid:
                self.x('INSERT OR IGNORE INTO entities(id, name) VALUES (?,?)', (eid, name[:80]))
                self.x('INSERT OR IGNORE INTO fact_entities(fid, eid) VALUES (?,?)', (f['id'], eid))

    def entities_of(self, fids):
        if not fids:
            return {}
        rows = self.q(f'SELECT fid, eid FROM fact_entities WHERE fid IN ({",".join("?" * len(fids))})', list(fids))
        out = {}
        for r in rows:
            out.setdefault(r['fid'], set()).add(r['eid'])
        return out

    def facts_with_entities(self, eids, exclude=(), k=6):
        if not eids:
            return []
        rows = self.q(f"SELECT DISTINCT f.* FROM fact_entities fe JOIN facts f ON f.id = fe.fid WHERE fe.eid IN ({','.join('?' * len(eids))}) "
                      "AND f.status='active' ORDER BY f.valid_from DESC LIMIT ?", list(eids) + [k + len(exclude)])
        return [r for r in rows if r['id'] not in set(exclude)][:k]

    def search_facts(self, text, k=20):
        fq = fts_query(text)
        if not fq:
            return []
        return self.q('SELECT f.*, bm25(facts_fts) AS score FROM facts_fts JOIN facts f ON f.id = facts_fts.fid '
                      "WHERE facts_fts MATCH ? AND f.status='active' ORDER BY score LIMIT ?", (fq, k))

    def facts_of_topic(self, topic_id):
        return self.q("SELECT f.* FROM facts f JOIN episodes e ON e.id = f.episode WHERE e.topic=? AND f.status='active' "
                      'ORDER BY f.rowid', (topic_id,))

    # ---------- topics ----------
    def upsert_topic(self, tid, name=None, desc=None, **levels):
        if not self.q('SELECT 1 FROM topics WHERE id=?', (tid,)):
            self.x('INSERT INTO topics(id, name, desc, updated) VALUES (?,?,?,?)', (tid, name or tid, desc or '', now()))
        sets, args = [], []
        for k, v in dict(name=name, desc=desc, **levels).items():
            if v is not None:
                sets.append(f'{k}=?'); args.append(json.dumps(v) if k == 'anchors' else v)
        if sets:
            self.x(f'UPDATE topics SET {", ".join(sets)}, updated=? WHERE id=?', args + [now(), tid])
        t = self.q('SELECT * FROM topics WHERE id=?', (tid,))[0]
        self.x('DELETE FROM topics_fts WHERE tid=?', (tid,))
        anchors = ' '.join(json.loads(t['anchors'] or '[]'))
        for lvl in ('small', 'medium', 'big'):
            self.x('INSERT INTO topics_fts(tid, level, text) VALUES (?,?,?)',
                   (tid, lvl, f'{t["name"]} {t["desc"]} {t[lvl] or ""} {anchors}'))

    def topics(self, status='active'):
        return self.q('SELECT * FROM topics WHERE status=? ORDER BY updated DESC', (status,))

    def search_topics(self, text, k=15):
        fq = fts_query(text)
        if not fq:
            return []
        return self.q('SELECT tid, level, bm25(topics_fts) AS score FROM topics_fts WHERE topics_fts MATCH ? ORDER BY score LIMIT ?',
                      (fq, k))

    # ---------- notes / decisions ----------
    def note(self, key, value=None):
        if value is None:
            r = self.q('SELECT value FROM notes WHERE key=?', (key,))
            return r[0]['value'] if r else None
        self.x('INSERT INTO notes(key, value, updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated',
               (key, value, now()))

    def decision(self, role, question, answer, p, mode='live'):   # R12: every Jev decision is logged
        self.x('INSERT INTO decisions(ts, role, question, answer, p, mode) VALUES (?,?,?,?,?,?)',
               (now(), role, question, json.dumps(answer), p, mode))
