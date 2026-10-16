//! The memory engine (architecture v0.8): log, sleep, Jev-routed recall, wake, pin/correct/forget, export.
//! Same behaviour, prompts and database as the Python engine.
use crate::config::{safe_space, Config};
use crate::jev::{self, Jev, Q};
use crate::llm::{make_writer, parse_json, Writer};
use crate::profiles::{get_profile, Profile};
use crate::redact::redact;
use crate::store::{entity_id, now, stop, Fact, LogRow, Store, Topic};
use crate::vectors::Vectors;
use regex::Regex;
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, OnceLock};

pub fn est(text: &str) -> usize {
    text.len() / 4
}

fn words(text: &str) -> HashSet<String> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"[a-z0-9_]{3,}").unwrap());
    re.find_iter(&text.to_lowercase()).map(|m| m.as_str().to_string()).filter(|w| !stop().contains(w.as_str())).collect()
}

fn jaccard(a: &HashSet<String>, b: &HashSet<String>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    a.intersection(b).count() as f64 / a.union(b).count() as f64
}

fn anchors_for(facts: &[Fact], cap: usize) -> Vec<String> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(concat!(
        r"`([^`]{2,60})`|([A-Za-z]:\\[^\s",
        "\"'|,;)",
        r"]+|~?/?[\w.\-]+(?:/[\w.\-]+)+)|(?:^|\W)(--[a-z][\w-]{2,})",
        r"|(\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b)",
        r"|(\b[\w-]+\.(?:ps1|py|json|md|ini|cfg|exe|bat|sh|html|js|ts|tsx|yaml|yml|toml|apk|log|txt|zip|sql|go|rs|java)\b)",
        r"|(\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b)|(\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b)|(\b[\w.-]*\d[\w.-]*\d[\w.-]*\b)")).unwrap());
    let mut seen = HashSet::new();
    let mut out = vec![];
    for f in facts {
        let text = format!("{} {}", f.claim, f.quote);
        for c in re.captures_iter(&text) {
            if let Some(m) = c.iter().skip(1).flatten().next() {
                let a = m.as_str().trim().trim_matches(|ch| ch == '.' || ch == ',').to_string();
                if a.chars().count() >= 3 && seen.insert(a.to_lowercase()) {
                    out.push(a);
                }
            }
        }
    }
    out.truncate(cap);
    out
}

fn rrf(rankings: &[Vec<String>]) -> Vec<String> {
    let mut score: HashMap<String, f64> = HashMap::new();
    let mut order: Vec<String> = vec![];
    for ranking in rankings {
        for (r, key) in ranking.iter().enumerate() {
            if !score.contains_key(key) {
                order.push(key.clone());
            }
            *score.entry(key.clone()).or_insert(0.0) += 1.0 / (60.0 + r as f64);
        }
    }
    order.sort_by(|a, b| score[b].partial_cmp(&score[a]).unwrap_or(std::cmp::Ordering::Equal));
    order
}

fn short(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

#[derive(Serialize, Default, Debug, Clone)]
pub struct Recall {
    pub context: String,
    pub exact: bool,
    pub level: String,
    pub tokens: usize,
    pub topics: Vec<String>,
    pub trace: Vec<Value>,
}

pub struct Memory {
    pub cfg: Config,
    pub space: String,
    pub profile: Profile,
    pub store: Arc<Store>,
    pub jev: Jev,
    pub vec: Vectors,
    writer: OnceLock<Box<dyn Writer>>,
}

const STATE_KINDS: [&str; 13] = ["preference", "personal_fact", "project", "plan", "account_state", "object_location", "map",
    "calibration", "convention", "command", "selector", "site_howto", "decision"];

enum Item {
    Fact(Fact),
    Msg(LogRow),
}

impl Memory {
    pub fn open(space: &str, profile: &str, cfg: Config) -> Result<Memory, String> {
        let space = safe_space(space);
        let store = Arc::new(Store::open(&cfg.db_path(&space))?);
        let jev = Jev::new(&cfg, Some(store.clone()));
        let vec = Vectors::new(cfg.vectors, store.clone());
        Ok(Memory { profile: get_profile(profile), space, store, jev, vec, cfg, writer: OnceLock::new() })
    }

    pub fn with_writer(self, w: Box<dyn Writer>) -> Memory {
        let _ = self.writer.set(w);
        self
    }

    fn writer(&self) -> &dyn Writer {
        self.writer.get_or_init(|| make_writer(&self.cfg)).as_ref()
    }

    // ------------------------------------------------------------------ awake
    /// R1: redact locally, then append. Never edited afterwards. Messages worth remembering become facts right away.
    pub fn log(&self, content: &str, role: &str, session: &str, meta: &Value, fast: Option<bool>) -> Result<i64, String> {
        let (clean, kinds) = redact(content);
        let lid = self.store.append(session, role, &clean, meta, &kinds)?;
        if fast.unwrap_or_else(|| self.worth_now(&clean, role)) {
            self.fast_extract(lid);
        }
        Ok(lid)
    }

    pub fn worth_now(&self, text: &str, role: &str) -> bool {
        if !(role == "user" || role == "fact") || text.len() > 3000 || text.len() < 12 {
            return false;
        }
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new(r"(?i)\b(i (?:prefer|like|love|hate|dislike|use|switched|moved|decided|want|always|never|am|'m|work|live)|my (?:name|birthday|wife|husband|partner|son|daughter|job|email|favorite|favourite)|remember (?:that|this)|from now on|call me|we decided|don't ever|do not ever|going forward|actually,? it'?s)\b").unwrap());
        match self.jev.noul(&json!({"message": short(text, 2000)}), jev::WORTH_NOW, "gate") {
            Some(p) => p >= 0.6,
            None => re.is_match(text),
        }
    }

    /// Real-time fact path: extract from one message (+ two before it for context), verify by quote, store now.
    pub fn fast_extract(&self, lid: i64) {
        let row = match self.store.log_by_id(lid) { Some(r) => r, None => return };
        let mut prev = self.store.recent_in_session(&row.session, lid, 2);
        prev.reverse();
        prev.push(row.clone());
        let win: Vec<(LogRow, String)> = prev.into_iter().map(|r| {
            let line = format!("[#{} {}] {}", r.id, r.role, short(&r.content, 2000));
            (r, line)
        }).collect();
        if let Err(e) = self.consolidate_episode(&row.session, &win, Some(format!("fast_{}", lid)), Some(lid), false) {
            eprintln!("[agentmem] fast path failed: {}", e);
        }
    }

    pub fn needs_memory(&self, message: &str) -> bool {
        match self.jev.noul(&json!({"message": message}), jev::NEEDS_MEMORY, "gate") {
            Some(p) => p >= 0.5,
            None => message.split_whitespace().count() >= 4,
        }
    }

    // ------------------------------------------------------------------ recall (v0.8)
    fn query_entities(&self, query: &str) -> Vec<String> {
        let ql = format!(" {} ", query.to_lowercase());
        let is_word = |c: char| c.is_alphanumeric() || c == '_';
        self.store.entity_names().into_iter().filter(|(_, name)| {
            let n = name.to_lowercase();
            if n.chars().count() < 3 {
                return false;
            }
            ql.match_indices(&n).any(|(i, _)| {
                let before = ql[..i].chars().last().map(|c| !is_word(c)).unwrap_or(true);
                let after = ql[i + n.len()..].chars().next().map(|c| !is_word(c)).unwrap_or(true);
                before && after
            })
        }).map(|(id, _)| id).collect()
    }

    /// Candidates from every layer, cheaply (code only).
    fn gather(&self, query: &str) -> (Vec<Fact>, Vec<LogRow>, Vec<Fact>, Option<Topic>) {
        let s = &self.store;
        let bm: Vec<Fact> = s.search_facts(query, 30);
        let mut byid: HashMap<String, Fact> = bm.iter().map(|f| (f.id.clone(), f.clone())).collect();
        let fids = rrf(&[bm.iter().map(|f| f.id.clone()).collect(), self.vec.search("fact", query, 30)]);
        let miss: Vec<String> = fids.iter().filter(|i| !byid.contains_key(*i)).cloned().collect();
        for f in s.facts_by_ids(&miss) {
            byid.insert(f.id.clone(), f);
        }
        let facts: Vec<Fact> = fids.iter().filter_map(|i| byid.get(i).cloned()).take(25).collect();

        let lb = s.search_log(query, 30);
        let mut lby: HashMap<String, LogRow> = lb.iter().map(|r| (r.id.to_string(), r.clone())).collect();
        let lids = rrf(&[lb.iter().map(|r| r.id.to_string()).collect(), self.vec.search("log", query, 30)]);
        let miss: Vec<i64> = lids.iter().filter(|i| !lby.contains_key(*i)).filter_map(|i| i.parse().ok()).collect();
        for r in s.logs_by_ids(&miss) {
            lby.insert(r.id.to_string(), r);
        }
        let logs: Vec<LogRow> = lids.iter().filter_map(|i| lby.get(i).cloned()).take(15).collect();

        let mut ents = vec![];
        let qents = self.query_entities(query);
        if !qents.is_empty() {
            let seen: HashSet<String> = facts.iter().map(|f| f.id.clone()).collect();
            let cand: HashMap<String, Fact> = s.facts_with_entities(&qents, &seen, 400).into_iter().map(|f| (f.id.clone(), f)).collect();
            let keys: Vec<String> = cand.keys().cloned().collect();
            ents = self.vec.rank("fact", query, &keys).into_iter().filter_map(|k| cand.get(&k).cloned()).take(30).collect();
        }

        let ep_topic = s.episode_topics();
        let mut votes: HashMap<String, f64> = HashMap::new();
        for (r, tid) in s.search_topics(query, 10).into_iter().enumerate() {
            *votes.entry(tid).or_insert(0.0) += 1.0 / (60.0 + r as f64);
        }
        for (r, f) in facts.iter().take(12).enumerate() {
            if let Some(t) = ep_topic.get(&f.episode) {
                if !t.is_empty() {
                    *votes.entry(t.clone()).or_insert(0.0) += 1.0 / (60.0 + r as f64);
                }
            }
        }
        let top = votes.into_iter().max_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal)).map(|x| x.0);
        (facts, logs, ents, top.and_then(|t| s.topic(&t)))
    }

    fn fact_text(f: &Fact) -> String {
        format!("({}) {}  evidence: \"{}\"", short(&f.valid_from, 10), f.claim, short(&f.quote, 160))
    }

    fn msg_text(r: &LogRow) -> String {
        format!("[{} log:{}] {}", short(&r.ts, 10), r.id, short(&r.content, 700))
    }

    /// v0.8: Jev-routed recall. Code gathers candidates from every layer; Jev (free, parallel) decides the question type,
    /// which summary level already answers it, and which facts and messages are part of the answer.
    pub fn recall(&self, query: &str, budget: usize) -> Recall {
        let s = &self.store;
        if s.count("SELECT COUNT(*) FROM facts") == 0 && s.count("SELECT COUNT(*) FROM log") == 0 {
            return Recall { level: "none".into(), ..Default::default() };
        }
        let (facts, logs, ents, topic) = self.gather(query);
        let mut items: Vec<(Item, String)> = facts.into_iter().chain(ents).map(|f| { let t = Self::fact_text(&f); (Item::Fact(f), t) }).collect();
        items.extend(logs.into_iter().map(|r| { let t = Self::msg_text(&r); (Item::Msg(r), t) }));
        if !self.jev.enabled {
            return self.recall_plain(query, budget, items, topic);
        }
        let levels: Vec<&str> = ["small", "medium", "big"].into_iter().filter(|lv| topic.as_ref().map(|t| !t.level(lv).is_empty()).unwrap_or(false)).collect();

        // all Jev work for this question at once, `jev_parallel` requests in flight
        let n = items.len();
        let next = std::sync::atomic::AtomicUsize::new(0);
        let results: std::sync::Mutex<Vec<(Option<f64>, Option<f64>)>> = std::sync::Mutex::new(vec![(None, None); n]);
        let mut sums: HashMap<&str, f64> = HashMap::new();
        let mut intent = String::from("single_fact");
        std::thread::scope(|sc| {
            let it = sc.spawn(|| {
                self.jev.choice(&json!({"question": query}), jev::INTENT_Q, &jev::INTENT_OPTS, "route").unwrap_or_else(|| "single_fact".into())
            });
            let sm: Vec<_> = levels.iter().map(|lv| {
                let text = short(topic.as_ref().unwrap().level(lv), 6000);
                let lv = *lv;
                sc.spawn(move || (lv, self.jev.noul(&json!({"question": query, "summary": text}), jev::SUMMARY_ENOUGH, "gate")))
            }).collect();
            for _ in 0..self.cfg.jev_parallel.max(1).min(n.max(1)) {
                sc.spawn(|| loop {
                    let i = next.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    if i >= n {
                        break;
                    }
                    let a = self.jev.many(&json!({"question": query, "memory": items[i].1}),
                        &[("rel", Q::Noul(jev::ITEM_RELEVANT.0, jev::ITEM_RELEVANT.1, jev::ITEM_RELEVANT.2)),
                          ("use", Q::Noul(jev::ITEM_USABLE.0, jev::ITEM_USABLE.1, jev::ITEM_USABLE.2))], "gate");
                    let get = |k: &str| a.as_ref().and_then(|m| m.get(k)).and_then(|v| v.get("noul")).and_then(|v| v.as_f64());
                    results.lock().unwrap()[i] = (get("rel"), get("use"));
                });
            }
            intent = it.join().unwrap_or_else(|_| "single_fact".into());
            for h in sm {
                if let Ok((lv, Some(p))) = h.join() {
                    sums.insert(lv, p);
                }
            }
        });
        let gated = results.into_inner().unwrap();
        let mut res = Recall { exact: intent == "exact_value", topics: topic.iter().map(|t| t.name.clone()).collect(), ..Default::default() };
        res.trace.push(json!({"intent": intent, "summaries": sums}));

        // overview: the smallest summary level that answers is the whole context (the small -> medium -> big ladder)
        if intent == "overview" {
            for lv in &levels {
                if sums.get(lv).copied().unwrap_or(0.0) >= self.cfg.summary_min_probability {
                    let t = topic.as_ref().unwrap();
                    res.context = format!("[topic {} / {}] {}", t.name, lv, t.level(lv));
                    res.level = lv.to_string();
                    res.tokens = est(&res.context);
                    return res;
                }
            }
        }
        // evidence: what Jev says is part of the answer, most useful first; lists keep every useful item
        let usable = |i: usize| gated[i].1.unwrap_or(0.0);
        let mut order: Vec<usize> = (0..n).collect();
        order.sort_by(|a, b| usable(*b).partial_cmp(&usable(*a)).unwrap_or(std::cmp::Ordering::Equal));
        let cap = if intent == "list" { 40 } else { 12 };
        let mut kept: Vec<usize> = order.iter().copied().filter(|&i| usable(i) >= self.cfg.item_min_probability).take(cap).collect();
        let backup: Vec<usize> = order.iter().copied().filter(|i| !kept.contains(i) && gated[*i].0.unwrap_or(0.0) >= 0.5).collect();

        let render = |sel: &[usize]| -> String {
            let fs: Vec<String> = sel.iter().filter(|&&i| matches!(items[i].0, Item::Fact(_))).map(|&i| format!("- {}", items[i].1)).collect();
            let mut ms: Vec<(i64, String)> = sel.iter().filter_map(|&i| match &items[i].0 { Item::Msg(r) => Some((r.id, items[i].1.clone())), _ => None }).collect();
            ms.sort_by_key(|m| m.0);
            let mut parts = vec![];
            if !fs.is_empty() {
                parts.push(format!("[facts]\n{}", fs.join("\n")));
            }
            if !ms.is_empty() {
                parts.push(format!("[original messages]\n{}", ms.into_iter().map(|m| m.1).collect::<Vec<_>>().join("\n")));
            }
            parts.join("\n\n")
        };
        let mut ctx = render(&kept);
        let mut level = "evidence";
        if kept.is_empty() || matches!(intent.as_str(), "single_fact" | "when" | "exact_value" | "list") {
            let p = if kept.is_empty() { Some(0.0) } else { self.jev.noul(&json!({"question": query, "evidence": ctx}), jev::ANSWERABLE, "gate") };
            if p.unwrap_or(0.0) < self.cfg.gate_min_probability && !backup.is_empty() {
                kept.extend(backup.iter().take(12));
                ctx = render(&kept);
                level = "evidence+";
            }
            res.trace.push(json!({"answerable": p}));
        }
        if intent != "exact_value" && sums.get("small").copied().unwrap_or(0.0) >= 0.3 {
            if let Some(t) = &topic {
                ctx = format!("[topic {} / small] {}\n\n{}", t.name, short(&t.small, 400), ctx);
            }
        }
        while est(&ctx) > budget {
            match ctx.rfind('\n') { Some(i) => ctx.truncate(i), None => break }
        }
        for &i in &kept {
            if let Item::Fact(f) = &items[i].0 {
                let _ = s.exec("UPDATE facts SET recalled = recalled + 1, last_used=? WHERE id=?", &[&now(), &f.id]);
            }
        }
        res.tokens = est(&ctx);
        res.context = ctx;
        res.level = level.into();
        res
    }

    /// Without Jev: evidence first (hybrid-ranked facts and messages), trimmed to the budget.
    fn recall_plain(&self, _query: &str, budget: usize, items: Vec<(Item, String)>, topic: Option<Topic>) -> Recall {
        let fs: Vec<String> = items.iter().filter(|i| matches!(i.0, Item::Fact(_))).take(15).map(|i| format!("- {}", i.1)).collect();
        let ms: Vec<String> = items.iter().filter(|i| matches!(i.0, Item::Msg(_))).take(10).map(|i| i.1.clone()).collect();
        let mut parts = vec![];
        if let Some(t) = &topic {
            if !t.small.is_empty() {
                parts.push(format!("[topic {} / small] {}", t.name, short(&t.small, 500)));
            }
        }
        if !fs.is_empty() {
            parts.push(format!("[facts]\n{}", fs.join("\n")));
        }
        if !ms.is_empty() {
            parts.push(format!("[original messages]\n{}", ms.join("\n")));
        }
        let mut ctx = parts.join("\n\n");
        while est(&ctx) > budget {
            match ctx.rfind('\n') { Some(i) => ctx.truncate(i), None => break }
        }
        Recall { tokens: est(&ctx), context: ctx, exact: true, level: "evidence".into(), topics: topic.iter().map(|t| t.name.clone()).collect(), trace: vec![] }
    }

    /// Meaning-search index over raw messages. Runs at sleep; call once to backfill an older memory.
    pub fn index_log(&self) {
        let rows = self.store.all_log_for_index();
        self.vec.index("log", rows.into_iter().map(|(id, c)| (id.to_string(), c)).collect());
    }

    /// Starting context: pinned user facts, recent topic smalls, then the wake note last (stable prefix first).
    pub fn wake(&self) -> String {
        let s = &self.store;
        let mut parts = vec![];
        let pins = s.facts("SELECT * FROM facts WHERE source='user' AND status='active' ORDER BY rowid DESC LIMIT 15", vec![]);
        if !pins.is_empty() {
            parts.push(format!("[things the user told me to remember]\n{}", pins.iter().map(|p| format!("- {}", p.claim)).collect::<Vec<_>>().join("\n")));
        }
        let mut lines = vec![];
        let mut used = 0;
        for t in s.topics() {
            if t.small.is_empty() {
                continue;
            }
            let line = format!("- {}: {}", t.name, t.small);
            if used + est(&line) > self.cfg.wake_tokens {
                break;
            }
            used += est(&line);
            lines.push(line);
        }
        if !lines.is_empty() {
            parts.push(format!("[memory topics]\n{}", lines.join("\n")));
        }
        if let Some(note) = s.note_get("wake_note") {
            parts.push(format!("[wake note from last sleep]\n{}", note));
        }
        if parts.is_empty() {
            return String::new();
        }
        format!("Long-term memory (agentmem, space \"{}\"). Summaries are orientation only: for exact values, use the memory_recall tool or open the cited evidence.\n\n{}",
                self.space, parts.join("\n\n"))
    }

    // ------------------------------------------------------------------ user edits
    /// A user-sourced fact (trust just below CLAUDE.md, R11).
    pub fn pin(&self, claim: &str, session: &str) -> Result<String, String> {
        let lid = self.log(&format!("[pinned by user] {}", claim), "user", session, &json!({}), Some(false))?;
        let fid = format!("pin_{}", lid);
        let topic = self.assign_topic(&[claim.to_string()], false).unwrap_or_else(|| "pinned".into());
        self.store.upsert_topic(&topic, Some(&topic), None, &[], None, None)?;
        self.store.exec("INSERT OR IGNORE INTO episodes(id, session, start_id, end_id, topic, claims_extracted, claims_rejected, created) VALUES (?,?,?,?,?,1,0,?)",
                        &[&format!("ep_pin_{}", lid), &session, &lid, &lid, &topic, &now()])?;
        self.store.add_fact(&json!({"id": fid, "episode": format!("ep_pin_{}", lid), "claim": claim, "kind": "preference", "source": "user",
                                    "quote": claim, "evidence": [format!("log:{}", lid)], "importance": 5}))?;
        self.store.exec("UPDATE topics SET dirty=1 WHERE id=?", &[&topic])?;
        Ok(fid)
    }

    pub fn correct(&self, fact_id: &str, claim: &str, session: &str) -> Result<String, String> {
        let new = self.pin(claim, session)?;
        self.store.exec("UPDATE facts SET status='superseded', superseded_by=?, valid_to=? WHERE id=?", &[&new, &now(), &fact_id])?;
        self.store.exec("INSERT INTO links(src, dst, type, note) VALUES (?,?,?,?)", &[&new, &fact_id, &"supersedes", &"user correction"])?;
        Ok(new)
    }

    /// Archive facts (R5). hard=true also tombstones the cited log rows (privacy path, R1).
    pub fn forget(&self, fact_id: Option<&str>, query: Option<&str>, hard: bool) -> Result<usize, String> {
        let rows = match fact_id {
            Some(id) => self.store.facts("SELECT * FROM facts WHERE id=?", vec![id.to_string().into()]),
            None => self.store.search_facts(query.unwrap_or(""), 5),
        };
        for f in &rows {
            self.store.exec("UPDATE facts SET status='archived' WHERE id=?", &[&f.id])?;
            self.store.exec("DELETE FROM facts_fts WHERE fid=?", &[&f.id])?;
            if hard {
                let ev: Vec<String> = serde_json::from_str(&f.evidence).unwrap_or_default();
                for e in ev {
                    if let Some(n) = e.strip_prefix("log:").and_then(|x| x.split('-').next()).and_then(|x| x.parse::<i64>().ok()) {
                        self.store.tombstone(n, "user asked to forget")?;
                    }
                }
            }
        }
        Ok(rows.len())
    }

    // ------------------------------------------------------------------ asleep
    /// Consolidate everything after the watermark: segment, extract, verify, link to topics, rebuild dirty topics, wake note.
    pub fn sleep(&self, session: Option<&str>, max_episodes: usize) -> Result<Value, String> {
        let s = &self.store;
        let rows = s.unconsolidated(session);
        let (mut episodes, mut claims, mut kept) = (0usize, 0usize, 0usize);
        let mut by_session: Vec<(String, Vec<LogRow>)> = vec![];
        for r in rows.iter().cloned() {
            match by_session.iter_mut().find(|(k, _)| *k == r.session) {
                Some((_, v)) => v.push(r),
                None => by_session.push((r.session.clone(), vec![r])),
            }
        }
        'outer: for (sess, srows) in by_session {
            let mut windows: Vec<Vec<(LogRow, String)>> = vec![];
            let mut cur: Vec<(LogRow, String)> = vec![];
            let mut size = 0;
            for r in srows {
                let line = format!("[#{} {}] {}", r.id, r.role, short(&r.content, 4000));
                if !cur.is_empty() && size + line.len() > self.cfg.episode_chars {
                    windows.push(std::mem::take(&mut cur));
                    size = 0;
                }
                size += line.len();
                cur.push((r, line));
            }
            if !cur.is_empty() {
                windows.push(cur);
            }
            for win in windows {
                if episodes >= max_episodes {
                    break 'outer;
                }
                let (k, c) = self.consolidate_episode(&sess, &win, None, None, true)?;
                kept += k;
                claims += c;
                episodes += 1;
                s.set_watermark(&sess, win.last().unwrap().0.id)?;
            }
        }
        let dirty = s.dirty_topics();
        for t in &dirty {
            self.rebuild_topic(t)?;
        }
        if episodes > 0 {
            self.index_log();
            self.wake_note()?;
        }
        Ok(json!({"log_rows": rows.len(), "episodes": episodes, "claims": claims, "kept": kept, "topics_rebuilt": dirty.len()}))
    }

    fn consolidate_episode(&self, session: &str, win: &[(LogRow, String)], ep_id: Option<String>, only_row: Option<i64>, allow_writer_topic: bool)
        -> Result<(usize, usize), String> {
        let s = &self.store;
        let text: String = win.iter().map(|(_, l)| l.as_str()).collect::<Vec<_>>().join("\n");
        let ep_id = ep_id.unwrap_or_else(|| format!("ep_{}_{}", session, win[0].0.id));
        let prompt = format!(
            "The attached file is one part of a log of work between a user and an AI agent. List the facts worth remembering for future sessions: {}. Up to 12 facts{}. \
             Each fact: \"claim\" (one self-contained sentence with enough context to be understood later; name people and things explicitly, no pronouns), \
             \"kind\" (one of: {}), \"quote\" (a short passage under 200 characters copied EXACTLY from the log that supports the claim), \
             \"entities\" (the specific people, places, projects, tools, files, devices or objects the fact is about, as short names). Return ONLY a JSON array.",
            self.profile.focus, if only_row.is_some() { " stated in the LAST message only (earlier lines are context)" } else { "" }, self.profile.kinds.join(", "));
        let raw = self.writer().complete(&prompt, &[text])?;
        let items: Vec<Value> = parse_json(&raw).and_then(|v| v.as_array().cloned()).unwrap_or_default()
            .into_iter().filter(|x| x.get("claim").and_then(|c| c.as_str()).map(|c| !c.is_empty()).unwrap_or(false)).collect();
        let mut kept: Vec<Value> = vec![];
        for (i, x) in items.iter().enumerate() {
            let quote = x.get("quote").and_then(|q| q.as_str()).unwrap_or("").trim().to_string();
            if quote.is_empty() {
                continue;
            }
            let q100 = short(&quote, 100).to_lowercase();
            let hit = win.iter().map(|(r, _)| r).filter(|r| only_row.map(|o| r.id == o).unwrap_or(true))
                .find(|r| r.content.to_lowercase().contains(&q100));
            let hit = match hit { Some(h) => h, None => continue };                         // R2/R3: no evidence -> not a fact
            let claim = x["claim"].as_str().unwrap_or("").to_string();
            let p = self.jev.noul(&json!({"claim": claim, "evidence": short(&hit.content, 2500)}), jev::VERIFY, "judge");
            if matches!(p, Some(v) if v < 0.5) {
                continue;
            }
            let meta: Value = serde_json::from_str(&hit.meta).unwrap_or(json!({}));
            let mut evidence = vec![json!(format!("log:{}", hit.id))];
            if let Some(ev) = meta.get("evidence").and_then(|v| v.as_array()) {
                evidence.extend(ev.iter().filter(|e| e.is_string()).cloned());
            }
            let ents: Vec<String> = x.get("entities").and_then(|e| e.as_array()).map(|a| a.iter()
                .filter_map(|e| match e { Value::String(s) => Some(s.clone()), Value::Number(n) => Some(n.to_string()), _ => None })
                .filter(|e| !e.trim().is_empty()).map(|e| short(&e, 80)).take(8).collect()).unwrap_or_default();
            let kind = x.get("kind").and_then(|k| k.as_str()).filter(|k| self.profile.kinds.contains(k)).unwrap_or(self.profile.kinds.last().unwrap());
            let source = if (hit.role == "user" || hit.role == "fact") && (self.profile.name == "chat" || self.profile.name == "generic") { "user" } else { "observed" };
            let keep_meta: serde_json::Map<String, Value> = ["cwd", "url", "sensor"].iter().filter_map(|k| meta.get(*k).map(|v| (k.to_string(), v.clone()))).collect();
            kept.push(json!({"id": format!("{}#f{}", ep_id, i + 1), "episode": ep_id, "claim": short(&claim, 500), "kind": kind, "source": source,
                             "quote": short(&quote, 300), "evidence": evidence, "verify_p": p, "entities": ents, "valid_from": hit.ts,
                             "scope": meta.get("scope").cloned().unwrap_or(json!({})), "meta": keep_meta}));
        }
        let kept: Vec<Value> = kept.into_iter().filter(|f| !self.duplicate(f)).collect();
        let claims: Vec<String> = kept.iter().map(|f| f["claim"].as_str().unwrap_or("").to_string()).collect();
        let mut topic = if kept.is_empty() { None } else { self.assign_topic(&claims, allow_writer_topic) };
        if !kept.is_empty() && topic.is_none() {
            s.upsert_topic("inbox", Some("inbox"), Some("recent facts not yet sorted into a topic"), &[], None, None)?;
            topic = Some("inbox".into());
        }
        if only_row.is_none() || !kept.is_empty() {
            let start = only_row.unwrap_or(win[0].0.id);
            let end = only_row.unwrap_or(win.last().unwrap().0.id);
            s.exec("INSERT OR REPLACE INTO episodes(id, session, start_id, end_id, topic, claims_extracted, claims_rejected, created) VALUES (?,?,?,?,?,?,?,?)",
                   &[&ep_id, &session, &start, &end, &topic, &(items.len() as i64), &((items.len() - kept.len()) as i64), &now()])?;
        }
        let mut stored = vec![];
        for f in kept {
            if self.resolve_contradictions(&f)? {
                s.add_fact(&f)?;
                stored.push(f);
            }
        }
        if let (Some(t), false) = (&topic, stored.is_empty()) {
            s.exec("UPDATE topics SET dirty=1 WHERE id=?", &[t])?;
        }
        self.vec.index("fact", stored.iter().map(|f| (f["id"].as_str().unwrap_or("").to_string(),
            format!("{} {}", f["claim"].as_str().unwrap_or(""), f["quote"].as_str().unwrap_or("")))).collect());
        Ok((stored.len(), items.len()))
    }

    /// Same evidence row and mostly the same words -> already stored (real-time path, then sleep).
    fn duplicate(&self, f: &Value) -> bool {
        let lid = f["evidence"][0].as_str().unwrap_or("");
        let fw = words(f["claim"].as_str().unwrap_or(""));
        self.store.claims_with_evidence(lid).iter().any(|g| jaccard(&fw, &words(g)) >= 0.5)
    }

    /// J6 with validity windows. Returns false if the new fact should not be stored (older still valid).
    fn resolve_contradictions(&self, new: &Value) -> Result<bool, String> {
        let s = &self.store;
        let mut cands: Vec<Fact> = vec![];
        let none = HashSet::new();
        for e in new["entities"].as_array().cloned().unwrap_or_default() {
            for g in s.facts_with_entities(&[entity_id(e.as_str().unwrap_or(""))], &none, 4) {
                if !cands.iter().any(|c| c.id == g.id) { cands.push(g); }
            }
        }
        let claim = new["claim"].as_str().unwrap_or("");
        for g in s.search_facts(claim, 4) {
            if !cands.iter().any(|c| c.id == g.id) { cands.push(g); }
        }
        let nw = words(claim);
        let (new_id, new_kind, new_source) = (new["id"].as_str().unwrap_or(""), new["kind"].as_str().unwrap_or(""), new["source"].as_str().unwrap_or(""));
        let new_from = new["valid_from"].as_str().unwrap_or("");
        for g in cands.into_iter().take(4) {
            if g.episode == new["episode"].as_str().unwrap_or("") || g.claim == claim {
                continue;
            }
            let ch = self.jev.choice(&json!({"older": {"claim": g.claim, "date": short(&g.valid_from, 10)}, "newer": {"claim": claim, "date": short(new_from, 10)}}),
                                     jev::CONTRADICTION_Q, &jev::CONTRADICTION_OPTS, "judge")
                .unwrap_or_else(|| {                                                      // no Jev: conservative plain-code rule
                    let same = new_kind == g.kind && STATE_KINDS.contains(&new_kind) && jaccard(&nw, &words(&g.claim)) >= 0.45;
                    if same && new_from >= g.valid_from.as_str() { "newer_replaces".into() } else { "unrelated".into() }
                });
            if ch == "older_still_valid" {
                return Ok(false);
            }
            if ch == "newer_replaces" {
                if g.source == "user" && new_source != "user" {                            // R11: lower trust never silently replaces higher trust
                    s.exec("INSERT INTO links(src, dst, type, note) VALUES (?,?,?,?)", &[&new_id, &g.id, &"contradicts", &"lower-trust conflict"])?;
                    continue;
                }
                let to = if new_from.is_empty() { now() } else { new_from.to_string() };
                s.exec("UPDATE facts SET status='superseded', superseded_by=?, valid_to=? WHERE id=?", &[&new_id, &to, &g.id])?;
                s.exec("DELETE FROM facts_fts WHERE fid=?", &[&g.id])?;
                s.exec("INSERT INTO links(src, dst, type, note) VALUES (?,?,?,?)", &[&new_id, &g.id, &"supersedes", &""])?;
            }
        }
        Ok(true)
    }

    fn assign_topic(&self, claims: &[String], allow_writer: bool) -> Option<String> {
        let s = &self.store;
        let existing = s.topics();
        if !existing.is_empty() {
            let descs: Vec<(String, String)> = existing.iter().take(250).map(|t| (t.id.clone(), short(if t.desc.is_empty() { &t.name } else { &t.desc }, 200))).collect();
            let mut opts: Vec<(&str, &str)> = descs.iter().map(|(a, b)| (a.as_str(), b.as_str())).collect();
            opts.push(("new_topic", "None of the existing topics fits these facts well."));
            match self.jev.choice(&json!({"facts": claims}), "Which existing memory topic do the facts in `facts` mainly belong to? Pick new_topic if none fits well.", &opts, "judge") {
                Some(c) if c != "new_topic" => return Some(c),
                Some(_) => {}
                None => {                                                                    // no Jev: best keyword overlap
                    let fw = words(&claims.join(" "));
                    let score = |t: &Topic| fw.intersection(&words(&format!("{} {} {}", t.name, t.desc, t.small))).count();
                    if let Some(best) = existing.iter().max_by_key(|t| score(t)) {
                        if score(best) >= 3 {
                            return Some(best.id.clone());
                        }
                    }
                }
            }
        }
        if !allow_writer {
            return None;
        }
        let listing = existing.iter().take(80).map(|t| format!("- {}: {}", t.id, t.desc)).collect::<Vec<_>>().join("\n");
        let raw = self.writer().complete(&format!(
            "Existing memory topics (id: description):\n{}\n\nFacts from a new episode are in the attached file. Pick the ONE existing topic these facts mainly belong to, \
             or create a new topic if none fits well. Topics should be about a subject (a project, device, tool, person, place, problem area), not a date. \
             Return ONLY JSON: {{\"topic\": \"<existing id or new_short_snake_case_name>\", \"new\": true|false, \"desc\": \"<one-line description if new>\"}}",
            if listing.is_empty() { "(none yet)".into() } else { listing }), &[claims.iter().map(|c| format!("- {}", c)).collect::<Vec<_>>().join("\n")]).ok()?;
        let r = parse_json(&raw).unwrap_or(json!({}));
        let raw_tid = r.get("topic").and_then(|t| t.as_str()).unwrap_or("general").to_lowercase();
        let tid: String = Regex::new(r"[^a-z0-9_]+").unwrap().replace_all(&raw_tid, "_").trim_matches('_').chars().take(40).collect();
        let tid = if tid.is_empty() { "general".to_string() } else { tid };
        if s.topic(&tid).is_none() {
            let desc = short(r.get("desc").and_then(|d| d.as_str()).unwrap_or(&tid), 200);
            let _ = s.upsert_topic(&tid, Some(&tid), Some(&desc), &[], None, None);
        }
        Some(tid)
    }

    fn rebuild_topic(&self, tid: &str) -> Result<(), String> {
        let facts = self.store.facts_of_topic(tid);
        let facts: Vec<Fact> = facts[facts.len().saturating_sub(80)..].to_vec();
        if facts.is_empty() {
            self.store.exec("UPDATE topics SET dirty=0 WHERE id=?", &[&tid])?;
            return Ok(());
        }
        let t = self.store.topic(tid).ok_or("no topic")?;
        let src = facts.iter().map(|f| format!("[{}] ({}) {}", f.id, f.kind, f.claim)).collect::<Vec<_>>().join("\n");
        let mut levels = vec![];
        for (lv, tokens, shape) in [("small", self.cfg.topic_small_tokens, "one or two sentences"),
                                    ("medium", self.cfg.topic_medium_tokens, "a short paragraph or a few bullets"),
                                    ("big", self.cfg.topic_big_tokens, "a detailed structured summary")] {
            let txt = self.writer().complete(&format!(
                "Write the {} memory summary for the topic \"{}\" from the verified facts in the attached file. Size: about {} tokens, {}. \
                 Keep the most useful specifics (values, paths, commands, decisions, outcomes). Use only these facts. Return only the summary text.",
                lv.to_uppercase(), t.name, tokens, shape), &[src.clone()])?;
            levels.push((lv, txt));
        }
        let anchors = anchors_for(&facts, self.cfg.anchors_max);
        self.store.upsert_topic(tid, None, None, &levels, Some(&anchors), Some(0))?;
        self.vec.index("topic", levels.iter().map(|(lv, txt)| (format!("{}:{}", tid, lv), txt.clone())).collect());
        Ok(())
    }

    fn wake_note(&self) -> Result<(), String> {
        let recent = self.store.facts("SELECT * FROM facts WHERE status='active' ORDER BY rowid DESC LIMIT 40", vec![]);
        if recent.is_empty() {
            return Ok(());
        }
        let note = self.writer().complete("From the recent facts in the attached file, write a short wake note for the next session: open problems, \
                                           unfinished work, and what to do next. At most 8 bullet points. Return only the bullets.",
                                          &[recent.iter().map(|r| format!("- {}", r.claim)).collect::<Vec<_>>().join("\n")])?;
        self.store.note_set("wake_note", &short(&note, 2000))?;
        Ok(())
    }

    // ------------------------------------------------------------------ export / status
    pub fn export(&self) -> String {
        let mut out = vec![format!("# Memory: {}\n", self.space)];
        let pins = self.store.facts("SELECT * FROM facts WHERE source='user' AND status='active'", vec![]);
        if !pins.is_empty() {
            out.push(format!("## Pinned by the user\n{}", pins.iter().map(|p| format!("- {}  <!-- {} -->", p.claim, p.id)).collect::<Vec<_>>().join("\n")));
        }
        for t in self.store.topics() {
            out.push(format!("## {}\n{}\n\n{}", t.name, t.small, t.medium));
        }
        if let Some(n) = self.store.note_get("wake_note") {
            out.push(format!("## Wake note\n{}", n));
        }
        out.join("\n\n") + "\n"
    }

    pub fn status(&self) -> Value {
        let c = |sql: &str| self.store.count(sql);
        json!({"space": self.space, "profile": self.profile.name, "db": self.store.path.to_string_lossy(),
               "log_rows": c("SELECT COUNT(*) FROM log"), "unconsolidated": self.store.unconsolidated(None).len(),
               "facts": c("SELECT COUNT(*) FROM facts WHERE status='active'"), "topics": c("SELECT COUNT(*) FROM topics WHERE status='active'"),
               "jev": self.jev.enabled, "vectors": self.vec.enabled, "writer": self.cfg.writer, "engine": "rust"})
    }
}
