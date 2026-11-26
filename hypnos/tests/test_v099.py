"""v0.9.9: a pause ends by itself and is never silent; compaction learns the chat first and hands its facts back."""
import json
import pytest
from hypnos import hooks
from hypnos.config import Config
from hypnos.daemon import Service, PAUSE_HOURS, _Stamped
from hypnos.llm import FakeWriter


@pytest.fixture
def svc(tmp_path, monkeypatch):
    c = Config(home=tmp_path, writer='fake', jev=False, vectors=False)
    c.spaces_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    return Service(c)


def test_pause_is_announced_once_and_ends_by_itself(svc):
    svc.handle('/pause', {'space': 'p'})
    assert svc.handle('/log', {'space': 'p', 'content': 'private thing', 'session': 's'})['paused']
    first = svc.handle('/recall', {'space': 'p', 'query': 'anything', 'session': 's'})['notice']
    assert 'paused' in first
    assert 'paused' not in (svc.handle('/recall', {'space': 'p', 'query': 'again', 'session': 's'})['notice'] or '')
    assert 'paused' in svc.handle('/wake', {'space': 'p', 'session': 's'})['context']
    m = svc.mem('p', 'coding')                                   # pretend the pause was set long ago
    m.store.x("UPDATE notes SET updated='2020-01-01T00:00:00Z' WHERE key='paused'")
    assert svc.handle('/log', {'space': 'p', 'content': 'normal thing again', 'session': 's'})['id']
    assert m.store.note('paused') == '0' and PAUSE_HOURS > 0


def test_compaction_waits_for_sleep_and_wake_returns_the_chat_facts(svc):
    for line in ('The staging server for project_falcon listens on port 8443',
                 'The release branch for project_falcon is release_2026_11'):
        svc.handle('/log', {'space': 'p', 'content': line, 'role': 'agent', 'session': 'chat1'})
    r = svc.handle('/compacted', {'space': 'p', 'session': 'chat1', 'wait': True})
    assert 'slept' in r
    ctx = svc.handle('/wake', {'space': 'p', 'session': 'chat1', 'source': 'compact'})['context']
    assert 'trust the newer one that has evidence' in ctx
    assert 'project_falcon' in ctx                               # this chat's own facts come back after the summary
    plain = svc.handle('/wake', {'space': 'p', 'session': 'chat1', 'source': 'startup'})['context']
    assert 'trust the newer one' not in plain


def test_precompact_prints_plain_instructions(capsys, monkeypatch):
    monkeypatch.setattr(hooks, 'ensure_running', lambda wait=0: False)
    hooks.claude('PreCompact', json.dumps({'session_id': 's', 'cwd': '.'}))
    out = capsys.readouterr().out
    assert out.strip().startswith('Long-term memory (hypnos) has just learned this chat')
    assert 'hookSpecificOutput' not in out
    hooks.claude('UserPromptSubmit', json.dumps({'session_id': 's', 'cwd': '.', 'prompt': ''}))


def test_log_lines_are_dated():
    import io
    buf = io.StringIO()
    s = _Stamped(buf)
    s.write('[hypnos] one\n[hypnos] two\n')
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2 and all(l[:4].isdigit() and '[hypnos]' in l for l in lines)


def test_failed_sleeps_are_reported_once_then_cleared(svc):
    m = svc.mem('p', 'coding')
    for _ in range(3):
        svc.sleep_failed('p', 'coding', RuntimeError('Cannot reach the API server'))
    note = svc.handle('/recall', {'space': 'p', 'query': 'x', 'session': 's'})['notice']
    assert 'failed to learn 3 times' in note and 'Cannot reach' in note
    assert 'failed to learn' not in (svc.handle('/recall', {'space': 'p', 'query': 'y', 'session': 's'})['notice'] or '')
    m.store.note('sleepfail', '')
    assert svc.health_note(m, 'other') == ''


def test_postcompact_clash_is_shown_on_the_next_message(svc, monkeypatch):
    svc.handle('/log', {'space': 'p', 'content': 'The team lead for project_falcon is Priya since 26 Nov', 'role': 'agent',
                        'session': 'c1'})
    svc.handle('/compacted', {'space': 'p', 'session': 'c1', 'wait': True})
    m = svc.mem('p', 'coding')
    monkeypatch.setattr(m.writer, 'complete', lambda p, a=(): '[{"summary": "lead is Tom", "memory": "lead is Priya"}]')
    svc.handle('/clashcheck', {'space': 'p', 'session': 'c1', 'summary': 'The team lead for project_falcon is Tom.',
                               'wait': True})
    note = svc.handle('/recall', {'space': 'p', 'query': 'who leads project_falcon', 'session': 'c1'})['notice']
    assert 'summary says "lead is Tom"' in note and 'memory says "lead is Priya"' in note
    assert 'summary says' not in (svc.handle('/recall', {'space': 'p', 'query': 'again', 'session': 'c1'})['notice'] or '')


def test_postcompact_hook_sends_the_summary(monkeypatch):
    sent = {}
    monkeypatch.setattr(hooks, 'ensure_running', lambda wait=0: True)
    monkeypatch.setattr(hooks, 'call', lambda path, payload=None, timeout=10: sent.update({path: payload}) or {})
    hooks.claude('PostCompact', json.dumps({'session_id': 's', 'cwd': '.', 'compact_summary': 'Summary: port 9171'}))
    assert sent['/clashcheck']['summary'] == 'Summary: port 9171'
