"""Adapters for assistants, automations, robotics, search, browsing and any custom agent. Each is a thin layer over Memory:
it decides what to log (with which evidence) and hands back recalled context. Use in-process (Memory) or
through the local service by passing use_service=True."""
from __future__ import annotations
import json
import time
from typing import Any

from ..client import call, ensure_running
from ..engine import Memory


class _Base:
    profile = 'generic'

    def __init__(self, space: str, session: str = 'default', use_service: bool = False, skip_own_session: bool = False,
                 **mem_kwargs):
        """skip_own_session=True: recall leaves out this session's own rows, for chat-style loops whose model still sees
        the whole conversation. Off by default: robots and automations run one long session and need their own past."""
        self.space, self.session, self.use_service = space, session, use_service
        self.skip = session if skip_own_session else None
        if use_service:
            ensure_running()
            self.mem = None
        else:
            self.mem = Memory(space, self.profile, **mem_kwargs)

    def _log(self, content, role, meta=None):
        if self.mem:
            return self.mem.log(content, role, self.session, meta)
        return (call('/log', {'space': self.space, 'profile': self.profile, 'session': self.session,
                              'role': role, 'content': content, 'meta': meta or {}}) or {}).get('id')

    def recall(self, query: str, budget: int = 3000) -> str:
        if self.mem:
            return self.mem.recall(query, budget, session=self.skip).context
        return (call('/recall', {'space': self.space, 'profile': self.profile, 'session': self.skip, 'query': query,
                                 'budget': budget}, timeout=30) or {}).get('context', '')

    def remember(self, fact: str):
        return self.mem.pin(fact, self.session) if self.mem else call('/pin', {'space': self.space, 'profile': self.profile, 'claim': fact})

    def sleep(self):
        """Consolidate now (e.g. at task end). Through the service it runs in the background."""
        if self.mem:
            return self.mem.sleep()
        return call('/sleep', {'space': self.space, 'profile': self.profile})


class AgentMemory(_Base):
    """Any custom agent loop (Agent SDK, LangGraph, your own): log turns and tool calls, prepend recalled memory."""
    profile = 'generic'

    def user(self, text): return self._log(text, 'user')
    def assistant(self, text): return self._log(text, 'agent')

    def tool(self, name: str, args: Any, result: Any):
        return self._log(f'{name} {json.dumps(args, default=str)[:1500]}\n-> {str(result)[:3000]}', 'tool', {'tool': name})

    def system_prompt(self, base: str, user_message: str, budget: int = 2500) -> str:
        ctx = self.recall(user_message, budget)
        return base + ('\n\n## Long-term memory (verify exact values against the quoted evidence)\n' + ctx if ctx else '')


class RobotMemory(_Base):
    """Robots: observations with time, pose and sensor; actions and outcomes; 'where is X' style recall.
    Object locations go stale fast (profile TTL), maps and calibration slowly."""
    profile = 'robotics'

    def observe(self, text: str, sensor: str = 'camera', pose: Any = None, objects: dict | None = None):
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
        body = f'[{stamp} {sensor}] {text}' + (f' | pose={pose}' if pose is not None else '') + \
               (f' | objects={json.dumps(objects)}' if objects else '')
        return self._log(body, 'observation', {'sensor': sensor, 'evidence': [f'obs:{sensor}@{stamp}']})

    def act(self, action: str, result: str, success: bool | None = None):
        tag = '' if success is None else (' SUCCESS' if success else ' FAILED')
        return self._log(f'action: {action} -> {result}{tag}', 'agent')

    def where_is(self, obj: str) -> str:
        return self.recall(f'Where was the {obj} last seen? Give its location and when.')


class SearchMemory(_Base):
    """Research / search agents: every fact keeps its URL and fetch date; claims go stale after the profile TTL."""
    profile = 'search'

    def query(self, q: str): return self._log(f'search: {q}', 'user')

    def result(self, url: str, title: str, text: str):
        day = time.strftime('%Y-%m-%d')
        return self._log(f'[{title}]({url})\n{text[:4000]}', 'tool', {'url': url, 'evidence': [f'url:{url}@{day}']})


class BrowsingMemory(_Base):
    """Browsing agents: per-site how-tos, selectors and failures. Page text is redacted before logging;
    never pass passwords here (the redactor catches common patterns, not everything)."""
    profile = 'browsing'

    def step(self, url: str, action: str, outcome: str, selector: str | None = None):
        body = f'on {url}: {action}' + (f' [selector: {selector}]' if selector else '') + f' -> {outcome}'
        return self._log(body, 'agent', {'url': url, 'evidence': [f'url:{url}']})

    def page(self, url: str, text: str):
        return self._log(f'page {url}\n{text[:3000]}', 'observation', {'url': url})

    def howto(self, site: str, task: str) -> str:
        return self.recall(f'How do I {task} on {site}? Steps, buttons and selectors that worked, and what failed.')


class AssistantMemory(AgentMemory):
    """Personal and work assistants (email, calendar, support, research helpers): preferences, people, promises and
    deadlines. Turns are logged like AgentMemory; `commitment` records a promise with its due date."""
    profile = 'assistant'

    def commitment(self, what: str, due: str | None = None, to: str | None = None):
        return self._log(f'commitment: {what}' + (f' (to {to})' if to else '') + (f', due {due}' if due else ''), 'agent')


class AutomationMemory(_Base):
    """Workflow and automation agents (n8n, Zapier, Make, RPA, scheduled jobs): what each run did, what failed and why,
    and the fix that worked, so the next run avoids the same mistake. Never pass secrets or tokens."""
    profile = 'automation'

    def run(self, workflow: str, step: str, outcome: str, ok: bool | None = None, endpoint: str | None = None):
        tag = '' if ok is None else (' OK' if ok else ' FAILED')
        meta = {'evidence': [f'run:{workflow}@{time.strftime("%Y-%m-%dT%H:%M:%S")}']}
        if endpoint:
            meta['url'] = endpoint
        return self._log(f'[{workflow}] {step}' + (f' ({endpoint})' if endpoint else '') + f' -> {outcome}{tag}', 'tool', meta)

    def fix(self, workflow: str, problem: str, fix: str):
        return self._log(f'[{workflow}] problem: {problem}; fix that worked: {fix}', 'agent')

    def before_run(self, workflow: str) -> str:
        return self.recall(f'What should I know before running the {workflow} workflow: recent failures, their fixes, limits and quirks?')
