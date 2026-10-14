"""Per-agent profiles. The engine, rules and Jev questions are shared; a profile only changes
what kinds of facts exist, what evidence looks like, and when a fact goes stale."""
from __future__ import annotations
import json
import subprocess
import time
from dataclasses import dataclass, field

from .proc import hidden


def _age_days(ts: str) -> float:
    try:
        return (time.time() - time.mktime(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S'))) / 86400
    except (ValueError, TypeError):
        return 0.0


@dataclass
class Profile:
    name: str
    kinds: list[str]
    focus: str                                   # what to extract, in plain words
    ttl_days: dict = field(default_factory=dict)  # kind -> days before a fact is 'stale' (R13-style freshness)

    def stale(self, fact) -> bool:
        ttl = self.ttl_days.get(fact['kind'])
        return ttl is not None and _age_days(fact['valid_from']) > ttl


class CodingProfile(Profile):
    """Code evidence 'code:path@commit#La-b' is stale once git shows the file changed since that commit (R13)."""

    def stale(self, fact) -> bool:
        meta = json.loads(fact['meta'] or '{}')
        for ev in json.loads(fact['evidence'] or '[]'):
            if ev.startswith('code:') and '@' in ev and meta.get('cwd'):
                path, rest = ev[5:].split('@', 1)
                commit = rest.split('#', 1)[0]
                try:
                    r = subprocess.run(['git', 'diff', '--quiet', commit, '--', path], cwd=meta['cwd'],
                                       capture_output=True, timeout=5, **hidden())
                    if r.returncode == 1:
                        return True
                except (OSError, subprocess.TimeoutExpired):
                    pass
        return super().stale(fact)


PROFILES = {
    'coding': CodingProfile(
        'coding', ['convention', 'command', 'gotcha', 'failed_approach', 'decision', 'outcome', 'event'],
        'file paths, commands, settings, conventions, decisions with reasons, fixes, gotchas and approaches that failed'),
    'chat': Profile(
        'chat', ['preference', 'personal_fact', 'project', 'decision', 'plan', 'event'],
        "the user's preferences, facts about the user and their life or work, ongoing projects, decisions and plans"),
    'search': Profile(
        'search', ['claim', 'source_quality', 'open_question', 'decision'],
        'claims found with their source URL, how reliable each source is, conflicts between sources, open questions',
        ttl_days={'claim': 30}),
    'browsing': Profile(
        'browsing', ['site_howto', 'selector', 'failed_approach', 'account_state', 'event'],
        'how to do things on specific websites (steps, buttons, selectors), what broke, and the state of the task. Never passwords.',
        ttl_days={'selector': 14, 'site_howto': 60}),
    'robotics': Profile(
        'robotics', ['object_location', 'map', 'skill', 'failure', 'calibration', 'safety', 'event'],
        'where objects were seen (with time), map and layout facts, skills that worked, failures and their causes, calibration values, safety notes',
        ttl_days={'object_location': 0.05, 'map': 30, 'calibration': 14}),
    'assistant': Profile(                     # v0.9: personal and work assistants (email, calendar, research, support)
        'assistant', ['preference', 'personal_fact', 'contact', 'commitment', 'decision', 'plan', 'event'],
        "the user's preferences and how they like things done, people and how to reach or address them, promises and "
        'deadlines, decisions with reasons, ongoing plans. Never passwords or payment details.',
        ttl_days={'commitment': 60}),
    'automation': Profile(                    # v0.9: workflow and automation agents (n8n, Zapier, Make, RPA, cron jobs)
        'automation', ['workflow', 'trigger', 'endpoint', 'failure', 'fix', 'schedule', 'limit', 'outcome'],
        'which workflows exist and what they do, triggers and schedules, API endpoints and their quirks, rate limits, '
        'failures with their causes and the fixes that worked, run outcomes. Never secrets or tokens.',
        ttl_days={'endpoint': 90, 'limit': 30}),
    'generic': Profile(
        'generic', ['event', 'decision', 'outcome', 'lesson', 'preference'],
        'specific values, names, decisions with reasons, outcomes, lessons and preferences'),
}


def get_profile(name: str) -> Profile:
    return PROFILES.get(name, PROFILES['generic'])
