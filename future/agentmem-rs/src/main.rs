//! agentmem (Rust engine): evidence-linked agent memory with sleep consolidation and Jev-routed recall.
//! © 2026 Luza Maharjan. All rights reserved.
mod config;
mod daemon;
mod engine;
mod jev;
mod llm;
mod profiles;
mod redact;
mod store;
mod vectors;

use config::Config;
use engine::Memory;
use serde_json::json;

const HELP: &str = "agentmem (rust engine)

usage:
  agentmem serve [--host H] [--port P]           run the local memory service (same HTTP API as the Python engine)
  agentmem recall <query> [--space S] [--budget N]
  agentmem log <text> [--space S] [--role R] [--session X]
  agentmem sleep [--space S]
  agentmem wake | status | export [--space S]
  agentmem pin <claim> [--space S]
  agentmem forget <query> [--space S] [--hard]
  agentmem index-log [--space S]                 build the meaning-search index over raw messages

common options: --profile coding|chat|search|browsing|robotics|generic, --vectors, --no-jev, --writer opencode|claude|openai|fake";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        println!("{}", HELP);
        return;
    }
    let mut opt = std::collections::HashMap::new();
    let mut pos = vec![];
    let mut flags = std::collections::HashSet::new();
    let mut i = 1;
    while i < args.len() {
        let a = &args[i];
        if let Some(k) = a.strip_prefix("--") {
            if matches!(k, "vectors" | "no-jev" | "hard") {
                flags.insert(k.to_string());
            } else if i + 1 < args.len() {
                opt.insert(k.to_string(), args[i + 1].clone());
                i += 1;
            }
        } else {
            pos.push(a.clone());
        }
        i += 1;
    }
    let mut cfg = Config::load();
    if flags.contains("vectors") { cfg.vectors = true; }
    if flags.contains("no-jev") { cfg.jev = false; }
    if let Some(w) = opt.get("writer") { cfg.writer = w.clone(); }
    if let Some(h) = opt.get("host") { cfg.host = h.clone(); }
    if let Some(p) = opt.get("port").and_then(|p| p.parse().ok()) { cfg.port = p; }
    let space = opt.get("space").cloned().unwrap_or_else(|| "default".into());
    let profile = opt.get("profile").cloned().unwrap_or_else(|| "generic".into());
    let text = pos.join(" ");

    let run = || -> Result<serde_json::Value, String> {
        if args[0] == "serve" {
            daemon::serve(cfg.clone())?;
            return Ok(json!(null));
        }
        let m = Memory::open(&space, &profile, cfg.clone())?;
        Ok(match args[0].as_str() {
            "recall" => serde_json::to_value(m.recall(&text, opt.get("budget").and_then(|b| b.parse().ok()).unwrap_or(6000))).unwrap(),
            "log" => json!({"id": m.log(&text, opt.get("role").map(String::as_str).unwrap_or("user"), opt.get("session").map(String::as_str).unwrap_or("default"), &json!({}), None)?}),
            "sleep" => m.sleep(None, 50)?,
            "wake" => json!({"context": m.wake()}),
            "status" => m.status(),
            "export" => json!({"markdown": m.export()}),
            "pin" => json!({"id": m.pin(&text, "default")?}),
            "forget" => json!({"forgotten": m.forget(None, Some(&text), flags.contains("hard"))?}),
            "index-log" => { m.index_log(); m.status() }
            other => return Err(format!("unknown command: {}\n\n{}", other, HELP)),
        })
    };
    match run() {
        Ok(v) if v.is_null() => {}
        Ok(v) => {
            if let Some(ctx) = v.get("context").and_then(|c| c.as_str()).filter(|_| args[0] == "recall" || args[0] == "wake") {
                println!("{}", ctx);
                if args[0] == "recall" {
                    eprintln!("[level {} · {} tokens · trace {}]", v["level"], v["tokens"], v["trace"]);
                }
            } else if let Some(md) = v.get("markdown").and_then(|c| c.as_str()) {
                println!("{}", md);
            } else {
                println!("{}", serde_json::to_string_pretty(&v).unwrap());
            }
        }
        Err(e) => {
            eprintln!("error: {}", e);
            std::process::exit(1);
        }
    }
}
