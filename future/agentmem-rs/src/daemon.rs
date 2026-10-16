//! Local memory service: one process keeps every memory space (and the vector model) loaded, so hooks and plugins stay
//! fast. JSON over HTTP on 127.0.0.1 only, the same endpoints as the Python service, so every integration works unchanged.
//! Also runs sleep when a space goes idle.
use crate::config::Config;
use crate::engine::Memory;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const IDLE_SLEEP_MINUTES: u64 = 30;   // sleep trigger: idle for this long with unconsolidated log
const MAX_AWAKE_ROWS: usize = 400;    // sleep trigger: this many unconsolidated rows

pub struct Service {
    cfg: Config,
    mems: Mutex<HashMap<String, Arc<Memory>>>,
    sleeping: Mutex<HashSet<String>>,
    last_log: Mutex<HashMap<String, Instant>>,
}

impl Service {
    pub fn new(cfg: Config) -> Arc<Service> {
        Arc::new(Service { cfg, mems: Mutex::new(HashMap::new()), sleeping: Mutex::new(HashSet::new()), last_log: Mutex::new(HashMap::new()) })
    }

    fn mem(&self, space: &str, profile: &str) -> Result<Arc<Memory>, String> {
        let mut mems = self.mems.lock().unwrap();
        if let Some(m) = mems.get(space) {
            if m.profile.name == profile || profile == "generic" {
                return Ok(m.clone());
            }
        }
        let m = Arc::new(Memory::open(space, profile, self.cfg.clone())?);
        mems.insert(space.to_string(), m.clone());
        Ok(m)
    }

    fn sleep_async(self: &Arc<Self>, space: &str, profile: &str, session: Option<String>) -> bool {
        if !self.sleeping.lock().unwrap().insert(space.to_string()) {
            return false;
        }
        let (me, space, profile) = (self.clone(), space.to_string(), profile.to_string());
        std::thread::spawn(move || {
            match me.mem(&space, &profile).and_then(|m| m.sleep(session.as_deref(), 50)) {
                Ok(_) => {}
                Err(e) => eprintln!("[agentmem] sleep failed for {}: {}", space, e),   // the watermark stays; next sleep retries
            }
            me.sleeping.lock().unwrap().remove(&space);
        });
        true
    }

    pub fn handle(self: &Arc<Self>, path: &str, d: &Value) -> Result<Value, (u16, String)> {
        let gs = |k: &str, def: &str| d.get(k).and_then(|v| v.as_str()).unwrap_or(def).to_string();
        let (space, profile) = (gs("space", "default"), gs("profile", "generic"));
        let m = self.mem(&space, &profile).map_err(|e| (500, e))?;
        let need = |k: &str| d.get(k).and_then(|v| v.as_str()).map(String::from).ok_or((404, format!("unknown or missing: '{}'", k)));
        let err = |e: String| (500u16, e);
        Ok(match path {
            "/log" => {
                let content = need("content")?;
                let role = gs("role", "user");
                let lid = m.log(&content, &role, &gs("session", "default"), d.get("meta").unwrap_or(&json!({})), Some(false)).map_err(err)?;
                let m2 = m.clone();
                std::thread::spawn(move || {                                   // logging never waits on the real-time fact path
                    if m2.worth_now(&content, &role) {
                        m2.fast_extract(lid);
                    }
                });
                self.last_log.lock().unwrap().insert(space.clone(), Instant::now());
                if m.store.unconsolidated(None).len() >= MAX_AWAKE_ROWS {
                    self.sleep_async(&space, &profile, None);
                }
                json!({"id": lid})
            }
            "/recall" => {
                let q = need("query")?;
                if d.get("gate").and_then(|v| v.as_bool()).unwrap_or(false) && !m.needs_memory(&q) {
                    json!({"context": "", "level": "skipped"})
                } else {
                    let budget = d.get("budget").and_then(|v| v.as_u64()).unwrap_or(6000) as usize;
                    serde_json::to_value(m.recall(&q, budget)).unwrap_or(json!({}))
                }
            }
            "/wake" => json!({"context": m.wake()}),
            "/sleep" => {
                let session = d.get("session").and_then(|v| v.as_str()).map(String::from);
                if d.get("wait").and_then(|v| v.as_bool()).unwrap_or(false) {
                    m.sleep(session.as_deref(), 50).map_err(err)?
                } else {
                    json!({"started": self.sleep_async(&space, &profile, session)})
                }
            }
            "/pin" => json!({"id": m.pin(&need("claim")?, &gs("session", "default")).map_err(err)?}),
            "/correct" => json!({"id": m.correct(&need("fact_id")?, &need("claim")?, &gs("session", "default")).map_err(err)?}),
            "/forget" => {
                let hard = d.get("hard").and_then(|v| v.as_bool()).unwrap_or(false);
                json!({"forgotten": m.forget(d.get("fact_id").and_then(|v| v.as_str()), d.get("query").and_then(|v| v.as_str()), hard).map_err(err)?})
            }
            "/status" => {
                let mut s = m.status();
                s["sleeping"] = json!(self.sleeping.lock().unwrap().contains(&space));
                s
            }
            "/export" => json!({"markdown": m.export()}),
            "/ping" => json!({"ok": true, "engine": "rust"}),
            other => return Err((404, format!("unknown or missing: '{}'", other))),
        })
    }

    fn idle_loop(self: Arc<Self>) {
        loop {
            std::thread::sleep(Duration::from_secs(60));
            let due: Vec<String> = self.last_log.lock().unwrap().iter()
                .filter(|(_, t)| t.elapsed() > Duration::from_secs(IDLE_SLEEP_MINUTES * 60)).map(|(s, _)| s.clone()).collect();
            for space in due {
                let m = self.mems.lock().unwrap().get(&space).cloned();
                if let Some(m) = m {
                    if !m.store.unconsolidated(None).is_empty() {
                        self.sleep_async(&space, m.profile.name, None);
                    }
                }
                self.last_log.lock().unwrap().remove(&space);
            }
        }
    }
}

pub fn serve(cfg: Config) -> Result<(), String> {
    let addr = format!("{}:{}", cfg.host, cfg.port);
    let server = tiny_http::Server::http(&addr).map_err(|e| e.to_string())?;
    let svc = Service::new(cfg.clone());
    let idle = svc.clone();
    std::thread::spawn(move || idle.idle_loop());
    println!("[agentmem] rust engine serving on http://{}  (memory in {})", addr, cfg.spaces_dir().display());
    for mut req in server.incoming_requests() {
        let svc = svc.clone();
        std::thread::spawn(move || {
            let mut body = String::new();
            let _ = req.as_reader().read_to_string(&mut body);
            let d: Value = serde_json::from_str(if body.trim().is_empty() { "{}" } else { &body }).unwrap_or(json!({}));
            let path = req.url().split('?').next().unwrap_or("/").to_string();
            let (code, out) = match svc.handle(&path, &d) {
                Ok(v) => (200, v),
                Err((c, e)) => (c, json!({"error": e})),
            };
            let resp = tiny_http::Response::from_string(out.to_string()).with_status_code(code)
                .with_header("Content-Type: application/json".parse::<tiny_http::Header>().unwrap());
            let _ = req.respond(resp);
        });
    }
    Ok(())
}
