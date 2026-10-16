//! Jev (TypeSafe System One) client for the gate, router and judge questions. Returns None when Jev is unavailable,
//! so every caller has a plain-code fallback and the engine works without a key.
use crate::config::Config;
use crate::store::Store;
use serde_json::{json, Map, Value};
use std::sync::Arc;
use std::time::Duration;

const API: &str = "https://api.typesafe.ai/v1/systemone";

pub struct Jev {
    key: String,
    pub enabled: bool,
    store: Option<Arc<Store>>,
    agent: ureq::Agent,
}

/// One typed question: noul (probability of true), choice (pick one), score (ordered levels).
pub enum Q<'a> {
    Noul(&'a str, &'a str, &'a str),
    Choice(&'a str, &'a [(&'a str, &'a str)]),
}

impl Jev {
    pub fn new(cfg: &Config, store: Option<Arc<Store>>) -> Jev {
        let mut key = std::env::var("TYPESAFE_API_KEY").unwrap_or_default();
        if key.is_empty() && cfg.jev {
            if let Ok(text) = std::fs::read_to_string(&cfg.jev_credentials) {
                for line in text.lines() {
                    if let Some(v) = line.strip_prefix("TYPESAFE_API_KEY=") {
                        key = v.trim().to_string();
                    }
                }
            }
        }
        let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(15)).build();
        Jev { enabled: cfg.jev && !key.is_empty(), key, store, agent }
    }

    /// Several questions about the same state in ONE request (they run in parallel on Jev's side, nearly free).
    pub fn many(&self, state: &Value, qs: &[(&str, Q)], role: &str) -> Option<Map<String, Value>> {
        if !self.enabled {
            return None;
        }
        let mut questions = Map::new();
        for (id, q) in qs {
            let v = match q {
                Q::Noul(ins, t, f) => json!({"type": "noul", "instructions": ins, "criteria": {"true": t, "false": f}}),
                Q::Choice(ins, opts) => {
                    let crit: Map<String, Value> = opts.iter().map(|(k, d)| (k.to_string(), json!(d))).collect();
                    json!({"type": "choice", "instructions": ins, "criteria": crit})
                }
            };
            questions.insert(id.to_string(), v);
        }
        let body = json!({"state": state, "model": "jev-latest", "questions": questions});
        let resp = self.agent.post(API).set("Authorization", &format!("Bearer {}", self.key)).send_json(body).ok()?;
        let v: Value = resp.into_json().ok()?;
        let answers = v.get("answers")?.as_object()?.clone();
        if let Some(st) = &self.store {
            for (qid, a) in &answers {
                let ans = a.get("choice").or_else(|| a.get("noul")).or_else(|| a.get("score")).cloned().unwrap_or(Value::Null);
                st.decision(role, qid, &ans, a.get("noul").or_else(|| a.get("confidence")).and_then(|x| x.as_f64()));
            }
        }
        Some(answers)
    }

    pub fn noul(&self, state: &Value, q: (&str, &str, &str), role: &str) -> Option<f64> {
        self.many(state, &[("q", Q::Noul(q.0, q.1, q.2))], role)?.get("q")?.get("noul")?.as_f64()
    }

    pub fn choice(&self, state: &Value, ins: &str, opts: &[(&str, &str)], role: &str) -> Option<String> {
        self.many(state, &[("q", Q::Choice(ins, opts))], role)?.get("q")?.get("choice")?.as_str().map(String::from)
    }
}

// The typed questions used by the engine (section 6 of the architecture). Same wording as the Python engine.
pub const ANSWERABLE: (&str, &str, &str) = (
    "Does the evidence in `evidence` (facts and original messages) contain everything needed to answer the question in `question` completely? For questions asking for a list or a count, all items must be present.",
    "The evidence answers the question completely.",
    "Part of the answer is missing, or the evidence is only on the same subject.");
pub const INTENT_Q: &str = "What kind of answer does the question in `question` need from a memory of past conversations?";
pub const INTENT_OPTS: [(&str, &str); 5] = [
    ("overview", "A general picture or gist: what someone was doing, working on, feeling or planning, in broad terms."),
    ("single_fact", "One specific fact: a name, thing, place, reason or yes/no."),
    ("list", "Several items or a count gathered from different moments (all X, how many X, what kinds of X)."),
    ("when", "A date or time of an event."),
    ("exact_value", "A precise value that must match exactly: a file path, command, number, ID, setting or quoted text."),
];
pub const SUMMARY_ENOUGH: (&str, &str, &str) = (
    "Does the summary in `summary` alone answer the question in `question` completely and specifically?",
    "The summary answers the question completely.",
    "The summary is missing the specific information asked for, or is only on the same subject.");
pub const ITEM_RELEVANT: (&str, &str, &str) = (
    "Is the memory in `memory` about the subject of the question in `question`?",
    "It is about the same subject.", "It is about something else.");
pub const ITEM_USABLE: (&str, &str, &str) = (
    "Does the memory in `memory` state information that is part of the answer to the question in `question`?",
    "It states part of the answer.", "It does not contain any part of the answer.");
pub const VERIFY: (&str, &str, &str) = (
    "Does the evidence text in `evidence` directly support the claim in `claim`? Judge only from that evidence.",
    "The evidence states or clearly shows what the claim says.",
    "The evidence does not show it, contradicts it, or the claim adds details not in the evidence.");
pub const NEEDS_MEMORY: (&str, &str, &str) = (
    "Would answering or acting on the message in `message` benefit from recalling earlier sessions with this user (past decisions, preferences, facts, work done before)?",
    "Earlier context would likely help.",
    "The message is self-contained or small talk.");
pub const WORTH_NOW: (&str, &str, &str) = (
    "Does the message in `message` state something worth remembering long-term (a preference, a decision, a fact about the user or their work, a correction, or a plan)?",
    "It states something durable worth remembering.",
    "Small talk, a question, or nothing durable.");
pub const CONTRADICTION_Q: &str = "Compare the older fact in `older` with the newer fact in `newer`. Are they about the same thing, and if so, which holds now?";
pub const CONTRADICTION_OPTS: [(&str, &str); 4] = [
    ("newer_replaces", "Same thing; the newer fact replaces or updates the older one."),
    ("both_true", "Related but both can be true at once (different context, time or detail)."),
    ("older_still_valid", "Same thing; the newer statement is mistaken or less reliable, the older one still holds."),
    ("unrelated", "They are about different things."),
];
