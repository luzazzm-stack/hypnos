import os
import pytest
from hypnos import Memory
from hypnos.config import Config
from hypnos.llm import FakeWriter
from hypnos.redact import redact


@pytest.fixture
def mem(tmp_path):
    cfg = Config(home=tmp_path, writer='fake', jev=False, vectors=False)
    cfg.spaces_dir.mkdir(parents=True, exist_ok=True)
    return Memory('test', 'coding', cfg=cfg, writer=FakeWriter())


def test_redaction_before_logging(mem):
    lid = mem.log('export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456 and password=hunter2secret', role='tool')
    row = mem.store.q('SELECT content, redacted FROM log WHERE id=?', (lid,))[0]
    assert 'sk-abcdef' not in row['content'] and 'hunter2secret' not in row['content']
    assert 'openai_style_key' in row['redacted'] or 'secret_assignment' in row['redacted']


def test_sleep_then_recall_exact_value(mem):
    mem.log('please fix the flaky login test', role='user', session='s1')
    mem.log('The scheduled task JackMicAutoSwitch switches the default capture device on jack plug', role='agent', session='s1')
    mem.log('Ran pytest -q tests/test_auth.py: 3 failed, token expiry checked before decode', role='tool', session='s1')
    rep = mem.sleep()
    assert rep['episodes'] == 1 and rep['kept'] >= 2 and rep['topics_rebuilt'] == 1
    assert mem.status()['unconsolidated'] == 0
    r = mem.recall('What is the name of the scheduled task that switches the microphone?')
    assert r.exact and 'JackMicAutoSwitch' in r.context
    assert r.level in ('evidence', 'evidence+', 'deep', 'small', 'medium', 'big')


def test_wake_pin_correct_forget(mem):
    fid = mem.pin('The user prefers answers in short bullet points')
    assert 'short bullet points' in mem.wake()
    new = mem.correct(fid, 'The user prefers answers as a short table')
    assert mem.store.q('SELECT status FROM facts WHERE id=?', (fid,))[0]['status'] == 'superseded'
    assert mem.forget(new, hard=True) == 1
    assert 'short table' not in mem.wake()


def test_no_double_consolidation(mem):
    mem.log('Build command is uv run pytest -q in folder hypnos/', session='s2')
    mem.sleep(); first = mem.status()['facts']
    mem.sleep()
    assert mem.status()['facts'] == first


@pytest.fixture
def chat(tmp_path):
    cfg = Config(home=tmp_path / 'c', writer='fake', jev=False, vectors=False)
    cfg.spaces_dir.mkdir(parents=True, exist_ok=True)
    return Memory('chat', 'chat', cfg=cfg, writer=FakeWriter())


def test_realtime_fact_path_without_sleep(chat):
    chat.log('I prefer Linux over macOS for all development work', role='user', session='c1')
    assert chat.status()['facts'] >= 1                       # stored immediately, no sleep
    assert 'Linux' in chat.recall('Which operating system do I prefer for development?').context


def test_realtime_then_sleep_does_not_duplicate(chat):
    chat.log('I prefer Linux over macOS for all development work', role='user', session='c1')
    n = chat.status()['facts']
    chat.sleep()
    assert chat.status()['facts'] == n


def test_newer_fact_replaces_older_with_history(chat):
    chat.log('My favorite code editor is VS Code', role='user', session='c1', meta={'ts': '2026-01-10T10:00:00Z'})
    chat.log('My favorite code editor is Neovim now', role='user', session='c2', meta={'ts': '2026-06-01T10:00:00Z'})
    ctx = chat.recall("What is the user's favorite code editor?").context
    assert 'Neovim' in ctx
    assert 'replaced earlier fact' in ctx and 'VS Code' in ctx and '2026-01-10' in ctx
    assert chat.store.q("SELECT COUNT(*) FROM facts WHERE status='superseded'")[0][0] == 1


def test_multi_hop_through_shared_entity(mem):
    mem.log('Priya_Sharma leads the Atlas_Project migration team', role='agent', session='m1')
    mem.log('The Atlas_Project deadline slipped to 2026-11-30 after the database outage', role='agent', session='m1')
    mem.sleep()
    ctx = mem.recall('Who leads the team whose deadline slipped after the outage?').context
    assert 'Priya_Sharma' in ctx and '2026-11-30' in ctx


def test_empty_memory_recall(mem):
    assert mem.recall('anything').context == ''
