//! Writer LLMs used during sleep (extract facts, write topic summaries, wake note). Every typed decision goes to Jev.
use crate::config::Config;
use regex::Regex;
use serde_json::{json, Value};
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub trait Writer: Send + Sync {
    fn complete(&self, prompt: &str, attachments: &[String]) -> Result<String, String>;
}

fn which(names: &[&str]) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        for n in names {
            let p = dir.join(n);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

/// Wait for a child with a timeout; kill the whole process tree on timeout (Windows shims start a second process).
fn run_with_timeout(mut cmd: Command, stdin: Option<&str>, secs: u64) -> Result<String, String> {
    cmd.stdout(Stdio::piped()).stderr(Stdio::null()).stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() });
    let mut child = cmd.spawn().map_err(|e| e.to_string())?;
    if let (Some(text), Some(mut si)) = (stdin, child.stdin.take()) {
        let _ = si.write_all(text.as_bytes());
    }
    let mut out = child.stdout.take().unwrap();
    let reader = std::thread::spawn(move || {
        let mut s = String::new();
        let _ = std::io::Read::read_to_string(&mut out, &mut s);
        s
    });
    let start = Instant::now();
    loop {
        if let Ok(Some(_)) = child.try_wait() {
            break;
        }
        if start.elapsed() > Duration::from_secs(secs) {
            if cfg!(windows) {
                let _ = Command::new("taskkill").args(["/T", "/F", "/PID", &child.id().to_string()]).output();
            }
            let _ = child.kill();
            return Err("timeout".into());
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    Ok(reader.join().unwrap_or_default())
}

/// Stock `opencode run` with an isolated, empty config (the free Zen tier only accepts genuine OpenCode).
pub struct OpencodeWriter {
    model: String,
    cfg_home: PathBuf,
    run_dir: PathBuf,
    exe: PathBuf,
}

impl OpencodeWriter {
    pub fn new(cfg: &Config) -> OpencodeWriter {
        let cfg_home = cfg.home.join("opencode-cfg");
        let _ = std::fs::create_dir_all(cfg_home.join("opencode"));
        let conf = cfg_home.join("opencode").join("opencode.jsonc");
        if !conf.exists() {
            let _ = std::fs::write(&conf, "{ \"$schema\": \"https://opencode.ai/config.json\" }\n");
        }
        let run_dir = cfg.home.join("opencode-run");
        let _ = std::fs::create_dir_all(&run_dir);
        // the real executable: a .cmd/.bat wrapper would cut every argument at the first newline
        let exe = which(&["opencode.exe", "opencode"]).unwrap_or_else(|| PathBuf::from("opencode"));
        OpencodeWriter { model: cfg.writer_model.clone(), cfg_home, run_dir, exe }
    }
}

impl Writer for OpencodeWriter {
    fn complete(&self, prompt: &str, attachments: &[String]) -> Result<String, String> {
        let mut files = vec![];
        for (i, a) in attachments.iter().enumerate() {
            let p = self.run_dir.join(format!("att_{}_{}_{}.txt", std::process::id(), chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0), i));
            std::fs::write(&p, a).map_err(|e| e.to_string())?;
            files.push(p);
        }
        let full = format!("Do not use any tools. Answer directly from the attached file(s) and this message. {}", prompt);
        let mut result = Err("opencode writer failed".to_string());
        for attempt in 0..5u64 {
            let mut cmd = Command::new(&self.exe);
            cmd.current_dir(&self.run_dir).env("XDG_CONFIG_HOME", &self.cfg_home).env("AGENTMEM_DISABLE", "1")
                .args(["run", "-m", &self.model, "--format", "json"]);
            for f in &files {
                cmd.arg("-f").arg(f);
            }
            cmd.arg("--").arg(&full);
            match run_with_timeout(cmd, None, 300) {
                Ok(out) => {
                    let mut texts = vec![];
                    let mut err = false;
                    for line in out.lines() {
                        if let Ok(ev) = serde_json::from_str::<Value>(line) {
                            match ev.get("type").and_then(|t| t.as_str()) {
                                Some("text") => texts.push(ev["part"]["text"].as_str().unwrap_or("").to_string()),
                                Some("error") => err = true,
                                _ => {}
                            }
                        }
                    }
                    if !texts.is_empty() && !err {
                        result = Ok(texts.join("\n").trim().to_string());
                        break;
                    }
                    std::thread::sleep(Duration::from_secs(10 * (attempt + 1)));
                }
                Err(_) => std::thread::sleep(Duration::from_secs(30)),
            }
        }
        for f in files {
            let _ = std::fs::remove_file(f);
        }
        result
    }
}

/// Claude Code in print mode (`claude -p`), e.g. with Haiku. Uses your Claude subscription.
pub struct ClaudeWriter {
    model: String,
    exe: PathBuf,
}

impl Writer for ClaudeWriter {
    fn complete(&self, prompt: &str, attachments: &[String]) -> Result<String, String> {
        let body = attachments.iter().enumerate().fold(prompt.to_string(), |acc, (i, a)| format!("{}\n\n--- attached file {} ---\n{}", acc, i + 1, a));
        let mut cmd = Command::new(&self.exe);
        cmd.env("AGENTMEM_DISABLE", "1").args(["-p", "--model", &self.model, "--output-format", "json"]);
        let out = run_with_timeout(cmd, Some(&body), 300)?;
        let v: Value = serde_json::from_str(&out).map_err(|e| e.to_string())?;
        Ok(v["result"].as_str().unwrap_or("").trim().to_string())
    }
}

/// Any OpenAI-compatible chat API: Ollama, LM Studio, OpenRouter, OpenAI, vLLM ...
pub struct OpenAIWriter {
    url: String,
    key: String,
    model: String,
}

impl Writer for OpenAIWriter {
    fn complete(&self, prompt: &str, attachments: &[String]) -> Result<String, String> {
        let body = attachments.iter().enumerate().fold(prompt.to_string(), |acc, (i, a)| format!("{}\n\n--- attached file {} ---\n{}", acc, i + 1, a));
        let mut req = ureq::post(&self.url).timeout(Duration::from_secs(300));
        if !self.key.is_empty() {
            req = req.set("Authorization", &format!("Bearer {}", self.key));
        }
        let v: Value = req.send_json(json!({"model": self.model, "messages": [{"role": "user", "content": body}], "temperature": 0}))
            .map_err(|e| e.to_string())?.into_json().map_err(|e| e.to_string())?;
        Ok(v["choices"][0]["message"]["content"].as_str().unwrap_or("").trim().to_string())
    }
}

/// Deterministic offline writer for tests: every log line with a concrete value becomes a fact quoting itself.
pub struct FakeWriter;

impl Writer for FakeWriter {
    fn complete(&self, prompt: &str, attachments: &[String]) -> Result<String, String> {
        let src = attachments.first().cloned().unwrap_or_default();
        let strip = Regex::new(r"^\[[^\]]*\]\s*").unwrap();
        if prompt.contains("List the facts worth remembering") {
            let concrete = Regex::new(r"\d|[/\\_]|=|:|[a-z][A-Z]").unwrap();
            let pref = Regex::new(r"(?i)\b(prefer|favou?rite|switched|use)\b").unwrap();
            let like = Regex::new(r"(?i)\b(prefer|favou?rite|switched|like)\b").unwrap();
            let ent = Regex::new(r"\b[A-Z][a-z]+[A-Z]\w*|\b\w+_\w+|\b[A-Z][a-z]{3,}\b").unwrap();
            let mut out = vec![];
            for line in src.lines() {
                let body = strip.replace(line.trim(), "").to_string();
                if body.chars().count() > 25 && (concrete.is_match(&body) || pref.is_match(&body)) {
                    let kind = if like.is_match(&body) { "preference" } else { "event" };
                    let ents: Vec<String> = ent.find_iter(&body).take(5).map(|m| m.as_str().to_string()).collect();
                    let c: String = body.chars().take(200).collect();
                    let q: String = body.chars().take(180).collect();
                    out.push(json!({"claim": c, "kind": kind, "quote": q, "entities": ents}));
                }
                if out.len() >= 12 {
                    break;
                }
            }
            return Ok(Value::Array(out).to_string());
        }
        if prompt.contains("Pick the ONE existing topic") {
            let w = Regex::new(r"[a-z]{4,}").unwrap();
            let lower = src.to_lowercase();
            let name = w.find(&lower).map(|m| m.as_str().to_string()).unwrap_or_else(|| "general".into());
            return Ok(json!({"topic": name, "new": true, "desc": format!("about {}", name)}).to_string());
        }
        let limit = Regex::new(r"about (\d+) tokens").unwrap().captures(prompt).and_then(|c| c[1].parse::<usize>().ok()).map(|n| n * 4).unwrap_or(600);
        let strip2 = Regex::new(r"^\[[^\]]*\]\s*(\([^)]*\)\s*)?").unwrap();
        let joined: Vec<String> = src.lines().filter(|l| !l.trim().is_empty()).map(|l| strip2.replace(l, "").to_string()).collect();
        Ok(joined.join(" ").chars().take(limit).collect())
    }
}

pub fn make_writer(cfg: &Config) -> Box<dyn Writer> {
    match cfg.writer.as_str() {
        "claude" => Box::new(ClaudeWriter {
            model: if cfg.writer_model.starts_with("opencode/") { "haiku".into() } else { cfg.writer_model.clone() },
            exe: which(&["claude.exe", "claude"]).unwrap_or_else(|| PathBuf::from("claude")),
        }),
        "openai" => Box::new(OpenAIWriter {
            url: format!("{}/chat/completions", cfg.openai_base_url.trim_end_matches('/')),
            key: std::env::var(&cfg.openai_api_key_env).unwrap_or_default(),
            model: cfg.writer_model.clone(),
        }),
        "fake" => Box::new(FakeWriter),
        _ => Box::new(OpencodeWriter::new(cfg)),
    }
}

/// First JSON array or object in a model reply (tolerates ```json fences and prose around it).
pub fn parse_json(text: &str) -> Option<Value> {
    let t = Regex::new(r"(?m)^```(?:json)?|```$").unwrap().replace_all(text.trim(), "").trim().to_string();
    for (o, c) in [('[', ']'), ('{', '}')] {
        if let (Some(i), Some(j)) = (t.find(o), t.rfind(c)) {
            if j > i {
                if let Ok(v) = serde_json::from_str::<Value>(&t[i..=j]) {
                    return Some(v);
                }
            }
        }
    }
    None
}
