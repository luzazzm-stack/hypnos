"""R1: secrets are removed locally, before anything is written to the log. Never sent to a hosted model to decide."""
from __future__ import annotations
import re

_PATTERNS = [
    ('private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)),
    ('aws_key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('github_token', re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b')),
    ('openai_style_key', re.compile(r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b')),
    ('slack_token', re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}\b')),
    ('google_key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')),
    ('bearer', re.compile(r'(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}')),
    # key = value / key: value for secret-looking names
    ('secret_assignment', re.compile(
        r'(?i)\b((?:api[_-]?key|secret|token|passw(?:or)?d|pwd|access[_-]?key|client[_-]?secret|auth)[A-Za-z0-9_]*'
        r'\s*[:=]\s*["\']?)([^\s"\',;]{6,})')),
    ('url_credentials', re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]{3,})(@)')),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, kinds_removed)."""
    kinds = []
    for kind, pat in _PATTERNS:
        def sub(m, kind=kind):
            kinds.append(kind)
            if kind in ('secret_assignment', 'bearer'):
                return m.group(1) + '[REDACTED]'
            if kind == 'url_credentials':
                return m.group(1) + '[REDACTED]' + m.group(3)
            return f'[REDACTED:{kind}]'
        text = pat.sub(sub, text)
    return text, sorted(set(kinds))
