//! SQLite storage for one memory space: raw log, episodes, facts, topics, links, decisions. FTS5 for keyword search.
//! The schema is identical to the Python engine, so both engines read and write the same database files.
use regex::Regex;
use rusqlite::{params, params_from_iter, Connection, OptionalExtension, Row};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

const SCHEMA: &str = r#"
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
"#;

pub const STOP: &str = "a an the of to in on for and or is are was were be been what which who whom whose when where why how did do does \
with by at from that this these those it its as into about my your our their was i you we they me us them \
there here then than so if not no yes can could would should will shall may might must have has had";

pub fn stop() -> &'static HashSet<&'static str> {
    static S: OnceLock<HashSet<&'static str>> = OnceLock::new();
    S.get_or_init(|| STOP.split_whitespace().collect())
}

pub fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

pub fn entity_id(name: &str) -> String {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"[^a-z0-9]+").unwrap());
    re.replace_all(&name.to_lowercase(), "_").trim_matches('_').chars().take(60).collect()
}

/// Free text -> a safe FTS5 OR-query of its content words.
pub fn fts_query(text: &str) -> String {
    static WORD: OnceLock<Regex> = OnceLock::new();
    static SPLIT: OnceLock<Regex> = OnceLock::new();
    let word = WORD.get_or_init(|| Regex::new(r"[A-Za-z0-9_][A-Za-z0-9_.\-/\\]*").unwrap());
    let split = SPLIT.get_or_init(|| Regex::new(r"[.\-/\\]").unwrap());
    let lower = text.to_lowercase();
    let mut seen = HashSet::new();
    let mut words = Vec::new();
    for m in word.find_iter(&lower) {
        for part in split.split(m.as_str()) {
            if part.chars().count() > 1 && !stop().contains(part) && seen.insert(part.to_string()) {
                words.push(format!("\"{}\"", part));
            }
        }
    }
    words.truncate(24);
    words.join(" OR ")
}

#[derive(Clone, Debug, Default)]
pub struct LogRow {
    pub id: i64,
    pub session: String,
    pub ts: String,
    pub role: String,
    pub content: String,
    pub meta: String,
}

#[derive(Clone, Debug, Default)]
pub struct Fact {
    pub id: String,
    pub episode: String,
    pub claim: String,
    pub kind: String,
    pub source: String,
    pub quote: String,
    pub evidence: String,
    pub valid_from: String,
    pub valid_to: String,
    pub superseded_by: String,
    pub status: String,
    pub meta: String,
}

#[derive(Clone, Debug, Default)]
pub struct Topic {
    pub id: String,
    pub name: String,
    pub desc: String,
    pub small: String,
    pub medium: String,
    pub big: String,
    pub anchors: String,
    pub dirty: i64,
}

impl Topic {
    pub fn level(&self, lv: &str) -> &str {
        match lv {
            "small" => &self.small,
            "medium" => &self.medium,
            _ => &self.big,
        }
    }
}

fn s(row: &Row, col: &str) -> String {
    row.get::<_, Option<String>>(col).ok().flatten().unwrap_or_default()
}

fn log_row(r: &Row) -> rusqlite::Result<LogRow> {
    Ok(LogRow { id: r.get("id")?, session: s(r, "session"), ts: s(r, "ts"), role: s(r, "role"), content: s(r, "content"), meta: s(r, "meta") })
}

fn fact_row(r: &Row) -> rusqlite::Result<Fact> {
    Ok(Fact {
        id: s(r, "id"), episode: s(r, "episode"), claim: s(r, "claim"), kind: s(r, "kind"), source: s(r, "source"),
        quote: s(r, "quote"), evidence: s(r, "evidence"), valid_from: s(r, "valid_from"), valid_to: s(r, "valid_to"),
        superseded_by: s(r, "superseded_by"), status: s(r, "status"), meta: s(r, "meta"),
    })
}

fn topic_row(r: &Row) -> rusqlite::Result<Topic> {
    Ok(Topic {
        id: s(r, "id"), name: s(r, "name"), desc: s(r, "desc"), small: s(r, "small"), medium: s(r, "medium"),
        big: s(r, "big"), anchors: s(r, "anchors"), dirty: r.get::<_, Option<i64>>("dirty")?.unwrap_or(0),
    })
}

fn marks(n: usize) -> String {
    vec!["?"; n].join(",")
}

pub struct Store {
    pub path: PathBuf,
    db: Mutex<Connection>,
}

type R<T> = Result<T, String>;
fn e(x: rusqlite::Error) -> String {
    x.to_string()
}

impl Store {
    pub fn open(path: &Path) -> R<Store> {
        if let Some(p) = path.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        let db = Connection::open(path).map_err(e)?;
        db.busy_timeout(std::time::Duration::from_secs(30)).map_err(e)?;
        db.execute_batch(SCHEMA).map_err(e)?;
        Ok(Store { path: path.to_path_buf(), db: Mutex::new(db) })
    }

    pub fn exec(&self, sql: &str, args: &[&dyn rusqlite::ToSql]) -> R<usize> {
        self.db.lock().unwrap().execute(sql, args).map_err(e)
    }

    pub fn count(&self, sql: &str) -> i64 {
        self.db.lock().unwrap().query_row(sql, [], |r| r.get(0)).unwrap_or(0)
    }

    fn logs(&self, sql: &str, args: Vec<rusqlite::types::Value>) -> Vec<LogRow> {
        let db = self.db.lock().unwrap();
        let mut st = match db.prepare(sql) { Ok(s) => s, Err(_) => return vec![] };
        st.query_map(params_from_iter(args), log_row).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    pub fn facts(&self, sql: &str, args: Vec<rusqlite::types::Value>) -> Vec<Fact> {
        let db = self.db.lock().unwrap();
        let mut st = match db.prepare(sql) { Ok(s) => s, Err(_) => return vec![] };
        st.query_map(params_from_iter(args), fact_row).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    fn strings(&self, sql: &str, args: Vec<rusqlite::types::Value>) -> Vec<String> {
        let db = self.db.lock().unwrap();
        let mut st = match db.prepare(sql) { Ok(s) => s, Err(_) => return vec![] };
        st.query_map(params_from_iter(args), |r| r.get::<_, Option<String>>(0).map(|x| x.unwrap_or_default()))
            .map(|it| it.filter_map(Result::ok).collect())
            .unwrap_or_default()
    }

    // ---------- log ----------
    pub fn append(&self, session: &str, role: &str, content: &str, meta: &Value, redacted: &[String]) -> R<i64> {
        let ts = meta.get("ts").and_then(|v| v.as_str()).map(String::from).unwrap_or_else(now);
        let db = self.db.lock().unwrap();
        db.execute("INSERT INTO log(session, ts, role, content, meta, redacted) VALUES (?,?,?,?,?,?)",
                   params![session, ts, role, content, meta.to_string(), json!(redacted).to_string()]).map_err(e)?;
        Ok(db.last_insert_rowid())
    }

    pub fn log_by_id(&self, id: i64) -> Option<LogRow> {
        self.logs("SELECT * FROM log WHERE id=?", vec![id.into()]).into_iter().next()
    }

    pub fn logs_by_ids(&self, ids: &[i64]) -> Vec<LogRow> {
        if ids.is_empty() {
            return vec![];
        }
        self.logs(&format!("SELECT * FROM log WHERE tombstone IS NULL AND id IN ({})", marks(ids.len())),
                  ids.iter().map(|&i| i.into()).collect())
    }

    pub fn recent_in_session(&self, session: &str, before: i64, n: i64) -> Vec<LogRow> {
        self.logs("SELECT * FROM log WHERE session=? AND id<? ORDER BY id DESC LIMIT ?", vec![session.to_string().into(), before.into(), n.into()])
    }

    pub fn unconsolidated(&self, session: Option<&str>) -> Vec<LogRow> {
        let mut sql = String::from("SELECT l.* FROM log l LEFT JOIN watermark w ON w.session = l.session \
                                    WHERE l.id > COALESCE(w.upto, 0) AND l.tombstone IS NULL");
        let mut args = vec![];
        if let Some(sess) = session {
            sql += " AND l.session = ?";
            args.push(sess.to_string().into());
        }
        self.logs(&(sql + " ORDER BY l.id"), args)
    }

    pub fn set_watermark(&self, session: &str, upto: i64) -> R<usize> {
        self.exec("INSERT INTO watermark(session, upto) VALUES (?,?) ON CONFLICT(session) DO UPDATE SET upto=excluded.upto", &[&session, &upto])
    }

    pub fn search_log(&self, text: &str, k: usize) -> Vec<LogRow> {
        let fq = fts_query(text);
        if fq.is_empty() {
            return vec![];
        }
        self.logs("SELECT l.* FROM log_fts JOIN log l ON l.id = log_fts.rowid WHERE log_fts MATCH ? AND l.tombstone IS NULL \
                   ORDER BY bm25(log_fts) LIMIT ?", vec![fq.into(), (k as i64).into()])
    }

    pub fn all_log_for_index(&self) -> Vec<(i64, String)> {
        self.logs("SELECT * FROM log WHERE tombstone IS NULL", vec![])
            .into_iter().filter(|r| !r.content.trim().is_empty()).map(|r| (r.id, r.content)).collect()
    }

    pub fn tombstone(&self, log_id: i64, reason: &str) -> R<()> {
        let t = json!({"deleted": now(), "reason": reason}).to_string();
        self.exec("UPDATE log SET content='', tombstone=? WHERE id=?", &[&t, &log_id])?;
        self.exec("UPDATE facts SET evidence_status='deleted' WHERE evidence LIKE ?", &[&format!("%log:{}%", log_id)])?;
        Ok(())
    }

    // ---------- facts ----------
    pub fn add_fact(&self, f: &Value) -> R<()> {
        let g = |k: &str| f.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let id = g("id");
        let claim = g("claim");
        let quote = g("quote");
        let ents: Vec<String> = f.get("entities").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
        let valid_from = { let v = g("valid_from"); if v.is_empty() { now() } else { v } };
        let kind = { let v = g("kind"); if v.is_empty() { "event".into() } else { v } };
        let source = { let v = g("source"); if v.is_empty() { "observed".into() } else { v } };
        self.exec("INSERT OR REPLACE INTO facts(id, episode, claim, kind, source, quote, evidence, valid_from, scope, importance, verify_p, meta) \
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  &[&id, &g("episode"), &claim, &kind, &source, &quote,
                    &f.get("evidence").cloned().unwrap_or(json!([])).to_string(), &valid_from,
                    &f.get("scope").cloned().unwrap_or(json!({})).to_string(),
                    &f.get("importance").and_then(|v| v.as_i64()).unwrap_or(3),
                    &f.get("verify_p").and_then(|v| v.as_f64()),
                    &f.get("meta").cloned().unwrap_or(json!({})).to_string()])?;
        self.exec("DELETE FROM facts_fts WHERE fid=?", &[&id])?;
        self.exec("INSERT INTO facts_fts(fid, text) VALUES (?,?)", &[&id, &format!("{} {} {}", claim, quote, ents.join(" "))])?;
        for name in ents {
            let eid = entity_id(&name);
            if !eid.is_empty() {
                let nm: String = name.chars().take(80).collect();
                self.exec("INSERT OR IGNORE INTO entities(id, name) VALUES (?,?)", &[&eid, &nm])?;
                self.exec("INSERT OR IGNORE INTO fact_entities(fid, eid) VALUES (?,?)", &[&id, &eid])?;
            }
        }
        Ok(())
    }

    pub fn facts_by_ids(&self, ids: &[String]) -> Vec<Fact> {
        if ids.is_empty() {
            return vec![];
        }
        self.facts(&format!("SELECT * FROM facts WHERE status='active' AND id IN ({})", marks(ids.len())),
                   ids.iter().map(|i| i.clone().into()).collect())
    }

    pub fn facts_with_entities(&self, eids: &[String], exclude: &HashSet<String>, k: usize) -> Vec<Fact> {
        if eids.is_empty() {
            return vec![];
        }
        let mut args: Vec<rusqlite::types::Value> = eids.iter().map(|x| x.clone().into()).collect();
        args.push(((k + exclude.len()) as i64).into());
        self.facts(&format!("SELECT DISTINCT f.* FROM fact_entities fe JOIN facts f ON f.id = fe.fid WHERE fe.eid IN ({}) \
                             AND f.status='active' ORDER BY f.valid_from DESC LIMIT ?", marks(eids.len())), args)
            .into_iter().filter(|f| !exclude.contains(&f.id)).take(k).collect()
    }

    pub fn search_facts(&self, text: &str, k: usize) -> Vec<Fact> {
        let fq = fts_query(text);
        if fq.is_empty() {
            return vec![];
        }
        self.facts("SELECT f.* FROM facts_fts JOIN facts f ON f.id = facts_fts.fid WHERE facts_fts MATCH ? AND f.status='active' \
                    ORDER BY bm25(facts_fts) LIMIT ?", vec![fq.into(), (k as i64).into()])
    }

    pub fn facts_of_topic(&self, tid: &str) -> Vec<Fact> {
        self.facts("SELECT f.* FROM facts f JOIN episodes e ON e.id = f.episode WHERE e.topic=? AND f.status='active' ORDER BY f.rowid",
                   vec![tid.to_string().into()])
    }

    pub fn replaced_by(&self, fid: &str) -> Vec<Fact> {
        self.facts("SELECT * FROM facts WHERE superseded_by=?", vec![fid.to_string().into()])
    }

    pub fn entity_names(&self) -> Vec<(String, String)> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare("SELECT id, name FROM entities").unwrap();
        st.query_map([], |r| Ok((s(r, "id"), s(r, "name")))).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    pub fn episode_topics(&self) -> HashMap<String, String> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare("SELECT id, topic FROM episodes").unwrap();
        st.query_map([], |r| Ok((s(r, "id"), s(r, "topic")))).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    pub fn claims_with_evidence(&self, log_ref: &str) -> Vec<String> {
        self.strings("SELECT claim FROM facts WHERE status!='archived' AND evidence LIKE ?", vec![format!("%\"{}\"%", log_ref).into()])
    }

    // ---------- topics ----------
    pub fn topic(&self, tid: &str) -> Option<Topic> {
        let db = self.db.lock().unwrap();
        db.query_row("SELECT * FROM topics WHERE id=?", [tid], topic_row).optional().ok().flatten()
    }

    pub fn topics(&self) -> Vec<Topic> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare("SELECT * FROM topics WHERE status='active' ORDER BY updated DESC").unwrap();
        st.query_map([], topic_row).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    pub fn upsert_topic(&self, tid: &str, name: Option<&str>, desc: Option<&str>, levels: &[(&str, String)], anchors: Option<&[String]>, dirty: Option<i64>) -> R<()> {
        if self.topic(tid).is_none() {
            self.exec("INSERT INTO topics(id, name, desc, updated) VALUES (?,?,?,?)", &[&tid, &name.unwrap_or(tid), &desc.unwrap_or(""), &now()])?;
        }
        if let Some(n) = name { self.exec("UPDATE topics SET name=? WHERE id=?", &[&n, &tid])?; }
        if let Some(d) = desc { self.exec("UPDATE topics SET desc=? WHERE id=?", &[&d, &tid])?; }
        for (lv, txt) in levels {
            if matches!(*lv, "small" | "medium" | "big") {
                self.exec(&format!("UPDATE topics SET {}=? WHERE id=?", lv), &[txt, &tid])?;
            }
        }
        if let Some(a) = anchors { self.exec("UPDATE topics SET anchors=? WHERE id=?", &[&json!(a).to_string(), &tid])?; }
        if let Some(d) = dirty { self.exec("UPDATE topics SET dirty=? WHERE id=?", &[&d, &tid])?; }
        self.exec("UPDATE topics SET updated=? WHERE id=?", &[&now(), &tid])?;
        let t = self.topic(tid).ok_or("topic vanished")?;
        self.exec("DELETE FROM topics_fts WHERE tid=?", &[&tid])?;
        let anc: Vec<String> = serde_json::from_str(&t.anchors).unwrap_or_default();
        for lv in ["small", "medium", "big"] {
            self.exec("INSERT INTO topics_fts(tid, level, text) VALUES (?,?,?)",
                      &[&tid, &lv, &format!("{} {} {} {}", t.name, t.desc, t.level(lv), anc.join(" "))])?;
        }
        Ok(())
    }

    pub fn dirty_topics(&self) -> Vec<String> {
        self.strings("SELECT id FROM topics WHERE dirty=1", vec![])
    }

    pub fn search_topics(&self, text: &str, k: usize) -> Vec<String> {
        let fq = fts_query(text);
        if fq.is_empty() {
            return vec![];
        }
        self.strings("SELECT tid FROM topics_fts WHERE topics_fts MATCH ? ORDER BY bm25(topics_fts) LIMIT ?", vec![fq.into(), (k as i64).into()])
    }

    // ---------- notes / decisions / vectors ----------
    pub fn note_get(&self, key: &str) -> Option<String> {
        self.strings("SELECT value FROM notes WHERE key=?", vec![key.to_string().into()]).into_iter().next()
    }

    pub fn note_set(&self, key: &str, value: &str) -> R<usize> {
        self.exec("INSERT INTO notes(key, value, updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                  &[&key, &value, &now()])
    }

    /// R12: every Jev decision is logged.
    pub fn decision(&self, role: &str, question: &str, answer: &Value, p: Option<f64>) {
        let _ = self.exec("INSERT INTO decisions(ts, role, question, answer, p, mode) VALUES (?,?,?,?,?,'live')",
                          &[&now(), &role, &question, &answer.to_string(), &p]);
    }

    pub fn vectors(&self, kind: &str) -> Vec<(String, Vec<u8>)> {
        let db = self.db.lock().unwrap();
        let mut st = db.prepare("SELECT ref, vec FROM vectors WHERE kind=?").unwrap();
        st.query_map([kind], |r| Ok((s(r, "ref"), r.get::<_, Vec<u8>>("vec")?))).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    pub fn vector_refs(&self, kind: &str) -> HashSet<String> {
        self.strings("SELECT ref FROM vectors WHERE kind=?", vec![kind.to_string().into()]).into_iter().collect()
    }

    pub fn put_vector(&self, kind: &str, r: &str, v: &[u8]) -> R<usize> {
        self.exec("INSERT OR REPLACE INTO vectors(kind, ref, vec) VALUES (?,?,?)", &[&kind, &r, &v])
    }
}
