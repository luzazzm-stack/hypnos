//! R1: secrets are removed locally, before anything is written to the log. Never sent to a hosted model to decide.
use regex::{Captures, Regex};
use std::sync::OnceLock;

fn patterns() -> &'static Vec<(&'static str, Regex)> {
    static P: OnceLock<Vec<(&'static str, Regex)>> = OnceLock::new();
    P.get_or_init(|| {
        vec![
            ("private_key", Regex::new(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----").unwrap()),
            ("aws_key", Regex::new(r"\bAKIA[0-9A-Z]{16}\b").unwrap()),
            ("github_token", Regex::new(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b").unwrap()),
            ("openai_style_key", Regex::new(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b").unwrap()),
            ("slack_token", Regex::new(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b").unwrap()),
            ("google_key", Regex::new(r"\bAIza[0-9A-Za-z_-]{35}\b").unwrap()),
            ("jwt", Regex::new(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b").unwrap()),
            ("bearer", Regex::new(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}").unwrap()),
            ("secret_assignment", Regex::new(
                r#"(?i)\b((?:api[_-]?key|secret|token|passw(?:or)?d|pwd|access[_-]?key|client[_-]?secret|auth)[A-Za-z0-9_]*\s*[:=]\s*["']?)([^\s"',;]{6,})"#).unwrap()),
            ("url_credentials", Regex::new(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]{3,})(@)").unwrap()),
        ]
    })
}

/// Returns (clean_text, kinds_removed).
pub fn redact(text: &str) -> (String, Vec<String>) {
    let mut out = text.to_string();
    let mut kinds: Vec<String> = Vec::new();
    for (kind, re) in patterns() {
        let k = *kind;
        let mut hit = false;
        out = re
            .replace_all(&out, |c: &Captures| {
                hit = true;
                match k {
                    "secret_assignment" | "bearer" => format!("{}[REDACTED]", &c[1]),
                    "url_credentials" => format!("{}[REDACTED]{}", &c[1], &c[3]),
                    _ => format!("[REDACTED:{}]", k),
                }
            })
            .into_owned();
        if hit {
            kinds.push(k.to_string());
        }
    }
    kinds.sort();
    kinds.dedup();
    (out, kinds)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn removes_secrets() {
        let (t, k) = redact("token=abcdef123456 and key sk-ant-abcdefghijklmnopqrstuvwx");
        assert!(t.contains("token=[REDACTED]"));
        assert!(t.contains("[REDACTED:openai_style_key]"));
        assert_eq!(k, vec!["openai_style_key", "secret_assignment"]);
    }
}
