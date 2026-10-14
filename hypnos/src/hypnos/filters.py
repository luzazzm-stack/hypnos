"""v0.9 save filter: things that must never become memory facts.

An AI model refusing or being unable to do something is not a fact about the user's work. Remembered, it makes later
sessions refuse the same task again ("the assistant can't do X"), so such claims are dropped before they are stored."""
from __future__ import annotations
import re

_AI = r"\b(?:the\s+)?(?:ai|assistant|agent|model|llm|bot|chatbot|claude|gpt|chatgpt|gemini|copilot|opencode|codex|muse|i)\b"
_REFUSE = (r"(?:refus\w*|declin\w*|won'?t|will\s+not|would\s+not|wouldn'?t|can'?not|can'?t|could\s+not|couldn'?t|"
           r"(?:is|was|are|were|am)\s+(?:not\s+)?(?:un)?able\s+to|(?:is|was)\s+not\s+(?:allowed|permitted)\s+to|"
           r"not\s+(?:allowed|permitted|able)\s+to|unable\s+to|apologi[sz]\w*|said\s+(?:it|they)\s+(?:can'?t|cannot|won'?t))")
_HELP = r"(?:help|assist|do|comply|provide|complete|perform|generate|write|create|share|access|fulfil+|answer|continue|support)"

PATTERNS = [
    re.compile(_AI + r"[^.;]{0,60}?\b" + _REFUSE + r"\b[^.;]{0,40}?\b" + _HELP + r"\b", re.I),      # "the assistant declined to write ..."
    re.compile(_AI + r"[^.;]{0,40}?\b(?:refus\w*|declin\w*)\b", re.I),                               # "Claude refused the request"
    re.compile(r"\b(?:request|task|prompt)\b[^.;]{0,40}?\b(?:was|got|is)\s+(?:refused|declined|rejected)\s+by\s+" + _AI, re.I),
    re.compile(r"\b(?:content|safety|usage)\s+(?:policy|policies|filter|guidelines?)\b[^.;]{0,60}?\b(?:block|prevent|refus|declin|not allow)", re.I),
    re.compile(r"^\s*(?:i'?m sorry|sorry,|i apologi[sz]e|as an ai\b)", re.I),
]


def is_refusal(claim: str) -> bool:
    """True when the claim is about an AI model refusing, declining or saying it cannot do a task."""
    return any(p.search(claim or '') for p in PATTERNS)
