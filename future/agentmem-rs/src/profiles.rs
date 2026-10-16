//! Per-agent profiles. The engine, rules and Jev questions are shared; a profile only changes what kinds of facts exist,
//! what to extract, and when a fact goes stale.
use crate::store::Fact;
use serde_json::Value;

#[derive(Clone, Debug)]
pub struct Profile {
    pub name: &'static str,
    pub kinds: &'static [&'static str],
    pub focus: &'static str,
    pub ttl_days: &'static [(&'static str, f64)],
}

fn age_days(ts: &str) -> f64 {
    let t = ts.get(..19).unwrap_or(ts);
    chrono::NaiveDateTime::parse_from_str(t, "%Y-%m-%dT%H:%M:%S")
        .map(|d| (chrono::Utc::now().naive_utc() - d).num_seconds() as f64 / 86400.0)
        .unwrap_or(0.0)
}

impl Profile {
    pub fn stale(&self, f: &Fact) -> bool {
        if self.name == "coding" {
            // code evidence 'code:path@commit#La-b' is stale once git shows the file changed since that commit (R13)
            let meta: Value = serde_json::from_str(&f.meta).unwrap_or(Value::Null);
            let evidence: Vec<String> = serde_json::from_str(&f.evidence).unwrap_or_default();
            if let Some(cwd) = meta.get("cwd").and_then(|v| v.as_str()) {
                for ev in evidence {
                    if let Some(rest) = ev.strip_prefix("code:") {
                        if let Some((path, tail)) = rest.split_once('@') {
                            let commit = tail.split('#').next().unwrap_or("");
                            if let Ok(o) = std::process::Command::new("git").args(["diff", "--quiet", commit, "--", path]).current_dir(cwd).output() {
                                if o.status.code() == Some(1) {
                                    return true;
                                }
                            }
                        }
                    }
                }
            }
        }
        self.ttl_days.iter().find(|(k, _)| *k == f.kind).map(|(_, ttl)| age_days(&f.valid_from) > *ttl).unwrap_or(false)
    }
}

pub fn get_profile(name: &str) -> Profile {
    match name {
        "coding" => Profile { name: "coding", kinds: &["convention", "command", "gotcha", "failed_approach", "decision", "outcome", "event"],
            focus: "file paths, commands, settings, conventions, decisions with reasons, fixes, gotchas and approaches that failed", ttl_days: &[] },
        "chat" => Profile { name: "chat", kinds: &["preference", "personal_fact", "project", "decision", "plan", "event"],
            focus: "the user's preferences, facts about the user and their life or work, ongoing projects, decisions and plans", ttl_days: &[] },
        "search" => Profile { name: "search", kinds: &["claim", "source_quality", "open_question", "decision"],
            focus: "claims found with their source URL, how reliable each source is, conflicts between sources, open questions", ttl_days: &[("claim", 30.0)] },
        "browsing" => Profile { name: "browsing", kinds: &["site_howto", "selector", "failed_approach", "account_state", "event"],
            focus: "how to do things on specific websites (steps, buttons, selectors), what broke, and the state of the task. Never passwords.",
            ttl_days: &[("selector", 14.0), ("site_howto", 60.0)] },
        "robotics" => Profile { name: "robotics", kinds: &["object_location", "map", "skill", "failure", "calibration", "safety", "event"],
            focus: "where objects were seen (with time), map and layout facts, skills that worked, failures and their causes, calibration values, safety notes",
            ttl_days: &[("object_location", 0.05), ("map", 30.0), ("calibration", 14.0)] },
        _ => Profile { name: "generic", kinds: &["event", "decision", "outcome", "lesson", "preference"],
            focus: "specific values, names, decisions with reasons, outcomes, lessons and preferences", ttl_days: &[] },
    }
}
