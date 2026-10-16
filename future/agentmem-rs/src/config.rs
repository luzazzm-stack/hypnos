//! Settings: ~/.agentmem/config.toml, overridable by AGENTMEM_* environment variables (same keys as the Python engine).
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct Config {
    pub home: PathBuf,
    /// writer LLM used during sleep: opencode | claude | openai | fake
    pub writer: String,
    pub writer_model: String,
    pub openai_base_url: String,
    pub openai_api_key_env: String,
    pub jev: bool,
    pub jev_credentials: PathBuf,
    pub gate_min_probability: f64,
    pub vectors: bool,
    pub episode_chars: usize,
    pub topic_small_tokens: usize,
    pub topic_medium_tokens: usize,
    pub topic_big_tokens: usize,
    pub jev_parallel: usize,
    pub summary_min_probability: f64,
    pub item_min_probability: f64,
    pub anchors_max: usize,
    pub wake_tokens: usize,
    pub host: String,
    pub port: u16,
}

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME")).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."))
}

impl Default for Config {
    fn default() -> Self {
        let home = std::env::var_os("AGENTMEM_HOME").map(PathBuf::from).unwrap_or_else(|| home_dir().join(".agentmem"));
        Config {
            home,
            writer: "opencode".into(),
            writer_model: "opencode/muse-spark-1.3-contributor-free".into(),
            openai_base_url: "http://localhost:11434/v1".into(),
            openai_api_key_env: "OPENAI_API_KEY".into(),
            jev: true,
            jev_credentials: home_dir().join(".typesafe").join("credentials"),
            gate_min_probability: 0.85,
            vectors: false,
            episode_chars: 12000,
            topic_small_tokens: 60,
            topic_medium_tokens: 300,
            topic_big_tokens: 1500,
            jev_parallel: 12,
            summary_min_probability: 0.7,
            item_min_probability: 0.5,
            anchors_max: 30,
            wake_tokens: 1500,
            host: "127.0.0.1".into(),
            port: 7439,
        }
    }
}

impl Config {
    pub fn spaces_dir(&self) -> PathBuf {
        self.home.join("spaces")
    }
    pub fn db_path(&self, space: &str) -> PathBuf {
        self.spaces_dir().join(format!("{}.db", safe_space(space)))
    }

    fn set(&mut self, key: &str, v: &str) {
        let b = |s: &str| matches!(s.to_lowercase().as_str(), "1" | "true" | "yes" | "on");
        match key {
            "home" => self.home = PathBuf::from(v),
            "writer" => self.writer = v.into(),
            "writer_model" => self.writer_model = v.into(),
            "openai_base_url" => self.openai_base_url = v.into(),
            "openai_api_key_env" => self.openai_api_key_env = v.into(),
            "jev" => self.jev = b(v),
            "jev_credentials" => self.jev_credentials = PathBuf::from(v),
            "gate_min_probability" => self.gate_min_probability = v.parse().unwrap_or(self.gate_min_probability),
            "vectors" => self.vectors = b(v),
            "episode_chars" => self.episode_chars = v.parse().unwrap_or(self.episode_chars),
            "topic_small_tokens" => self.topic_small_tokens = v.parse().unwrap_or(self.topic_small_tokens),
            "topic_medium_tokens" => self.topic_medium_tokens = v.parse().unwrap_or(self.topic_medium_tokens),
            "topic_big_tokens" => self.topic_big_tokens = v.parse().unwrap_or(self.topic_big_tokens),
            "jev_parallel" => self.jev_parallel = v.parse().unwrap_or(self.jev_parallel),
            "summary_min_probability" => self.summary_min_probability = v.parse().unwrap_or(self.summary_min_probability),
            "item_min_probability" => self.item_min_probability = v.parse().unwrap_or(self.item_min_probability),
            "anchors_max" => self.anchors_max = v.parse().unwrap_or(self.anchors_max),
            "wake_tokens" => self.wake_tokens = v.parse().unwrap_or(self.wake_tokens),
            "host" => self.host = v.into(),
            "port" => self.port = v.parse().unwrap_or(self.port),
            _ => {}
        }
    }

    pub fn load() -> Config {
        let mut cfg = Config::default();
        let path = cfg.home.join("config.toml");
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(toml::Value::Table(t)) = text.parse::<toml::Value>() {
                for (k, v) in t {
                    let s = match v {
                        toml::Value::String(s) => s,
                        other => other.to_string(),
                    };
                    cfg.set(&k, &s);
                }
            }
        }
        const KEYS: [&str; 20] = ["home", "writer", "writer_model", "openai_base_url", "openai_api_key_env", "jev", "jev_credentials",
            "gate_min_probability", "vectors", "episode_chars", "topic_small_tokens", "topic_medium_tokens", "topic_big_tokens",
            "jev_parallel", "summary_min_probability", "item_min_probability", "anchors_max", "wake_tokens", "host", "port"];
        for k in KEYS {
            if let Ok(v) = std::env::var(format!("AGENTMEM_{}", k.to_uppercase())) {
                cfg.set(k, &v);
            }
        }
        let _ = std::fs::create_dir_all(cfg.spaces_dir());
        cfg
    }
}

pub fn safe_space(space: &str) -> String {
    let re = regex::Regex::new(r"[^A-Za-z0-9_.-]+").unwrap();
    let s: String = re.replace_all(space, "-").trim_matches('-').chars().take(80).collect();
    if s.is_empty() { "default".into() } else { s }
}

pub fn exists(p: &Path) -> bool {
    p.exists()
}
