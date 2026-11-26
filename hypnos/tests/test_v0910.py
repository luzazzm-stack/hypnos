"""v0.9.10: writer routing (usage-limit rests kept on disk, offline put-off, answer checks, per-sleep writer log),
a small starting context with the user's rules first, and the one-line "other chats" note."""
import json
import time
import pytest
from hypnos import Memory
from hypnos.config import Config
from hypnos.daemon import Service, GLOBAL_SPACE
from hypnos.engine import WAKE_MAX_CHARS
from hypnos import llm
from hypnos.llm import ChainWriter, FakeWriter, WriterOffline, WritersResting, classify_error, rest_seconds, read_key

CLAUDE_LIMIT = "claude writer failed: You've hit your 5-hour limit · resets 6:45pm (Asia/Kathmandu)"


@pytest.fixture
def cfg(tmp_path):
    c = Config(home=tmp_path, writer='fake', jev=False, vectors=False)
    c.spaces_dir.mkdir(parents=True, exist_ok=True)
    ChainWriter.down.clear()
    ChainWriter.offline_until = 0.0
    yield c
    ChainWriter.down.clear()
    ChainWriter.offline_until = 0.0


class Fails:
    def __init__(self, msg):
        self.msg, self.calls = msg, 0

    def complete(self, p, a=()):
        self.calls += 1
        raise RuntimeError(self.msg)


class Says:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def complete(self, p, a=()):
        self.calls += 1
        return self.text


def chain(cfg, links):
    cfg.writer_chain = []
    w = ChainWriter(cfg)
    w.links = links
    return w


def test_errors_are_classified_and_reset_times_parsed():
    assert classify_error(CLAUDE_LIMIT) == 'limit'
    assert classify_error("You've hit your session limit · resets 11:05pm (Asia/Kathmandu)") == 'limit'
    assert classify_error('HTTP 429: {"error": {"message": "Rate limit reached. Please try again in 1m30.5s"}}') == 'limit'
    assert classify_error("API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)") == 'offline'
    assert classify_error('<urlopen error [Errno 11001] getaddrinfo failed>') == 'offline'
    assert classify_error('claude writer failed: mp4q6HCYKERNEL32.DLL') == 'other'
    secs = rest_seconds(CLAUDE_LIMIT, 'claude:haiku')
    assert 60 < secs <= 86400 + 60                                   # until the next 6:45pm, never more than a day
    assert 90 <= rest_seconds('Please try again in 1m30.5s', 'groq:x') <= 95
    assert 37 <= rest_seconds('"retryDelay": "37s"', 'gemini:x') <= 40
    assert rest_seconds('quota exceeded', 'claude:sonnet') == 3600 and rest_seconds('quota exceeded', 'groq:x') == 600


def test_claude_limit_rests_every_claude_link_and_survives_a_restart(cfg):
    haiku, sonnet, groq = Fails(CLAUDE_LIMIT), Says('never asked'), Says('[]')
    w = chain(cfg, [('claude:haiku', haiku), ('claude:sonnet', sonnet), ('groq:model', groq)])
    assert w.complete('x') == '[]' and w.last == 'groq:model'
    assert sonnet.calls == 0                                         # the account limit covers Sonnet too
    assert 'claude' in json.loads((cfg.home / 'writer-rest.json').read_text())
    ChainWriter.down.clear()                                         # a restart forgets the short rests ...
    w2 = chain(cfg, [('claude:haiku', haiku), ('claude:sonnet', sonnet), ('groq:model', groq)])
    assert w2.complete('x') == '[]' and haiku.calls == 1             # ... but not the limit: Claude is not asked again
    assert w2.tally['groq:model'] == 1


def test_every_writer_resting_puts_the_sleep_off(cfg):
    w = chain(cfg, [('claude:haiku', Fails(CLAUDE_LIMIT)), ('groq:model', Fails('HTTP 429: Too Many Requests'))])
    with pytest.raises(RuntimeError):
        w.complete('x')
    with pytest.raises(WritersResting):
        w.complete('x')


def test_offline_stops_the_chain_at_once(cfg):
    groq = Says('[]')
    w = chain(cfg, [('claude:haiku', Fails("API Error: Can't reach the API server (ENOTFOUND)")), ('groq:model', groq)])
    with pytest.raises(WriterOffline):
        w.complete('x')
    assert groq.calls == 0 and not (cfg.home / 'writer-rest.json').exists()   # no rest: Claude's limit is not the problem
    with pytest.raises(WriterOffline):                               # later calls wait without asking anyone
        w.complete('x')
    assert groq.calls == 0


def test_a_fallback_answer_must_pass_the_same_check(cfg):
    bad, good = Says('Sure! Here are the facts: none'), Says('[{"claim": "x", "quote": "x"}]')
    w = chain(cfg, [('claude:haiku', Fails(CLAUDE_LIMIT)), ('groq:model', bad), ('gemini:model', good)])
    assert w.complete('x', check=llm.is_json_list) == good.text and w.last == 'gemini:model'
    w = chain(cfg, [('groq:model', bad), ('gemini:model', Fails('HTTP 500'))])
    assert w.complete('x', check=llm.is_json_list) == bad.text       # the first writer's own answer: as before v0.9.10
    w = chain(cfg, [('gemini:model', Fails('HTTP 500 again')), ('groq:model', bad)])
    ChainWriter.down.clear()
    with pytest.raises(RuntimeError):                                # a fallback's invalid answer is never used
        w.complete('x', check=llm.is_json_list)


def test_each_sleep_logs_the_writer_that_did_the_work(cfg, capsys):
    w = chain(cfg, [('claude:haiku', Fails(CLAUDE_LIMIT)), ('groq:model', FakeWriter())])
    mem = Memory('test', 'coding', cfg=cfg, writer=w)
    mem.log('The deploy script deploy_site.py pushes to the staging_bucket on port 8443', role='agent', session='s1')
    rep = mem.sleep()
    assert rep['kept'] >= 1 and set(rep['writers']) == {'groq:model'}
    out = capsys.readouterr().out
    assert 'sleep done for test' in out and 'writer: groq:model x' in out


def test_an_offline_sleep_is_put_off_not_counted_as_failed(cfg, monkeypatch, capsys):
    svc = Service(cfg)
    m = svc.mem('proj', 'coding')
    m.log('The staging server for project_falcon listens on port 8443', role='agent', session='s1')

    def offline(*a, **k):
        raise WriterOffline('offline (claude:haiku: ENOTFOUND)')
    monkeypatch.setattr(m, 'sleep', offline)
    svc.sleep_async('proj', 'coding', 's1')
    for _ in range(50):
        if 'put off' in capsys.readouterr().out or not svc.queued:
            break
        time.sleep(0.05)
    time.sleep(0.1)
    assert not m.store.note('sleepfail')                             # no "failed to learn" warning for being offline


def test_keys_come_from_the_environment_or_the_keys_file(tmp_path, monkeypatch):
    f = tmp_path / 'keys.env'
    f.write_text('# keys\nGROQ_API_KEY = gsk_from_file\nOTHER="quoted"\n', encoding='utf-8')
    c = Config(home=tmp_path, keys_file=str(f))
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    assert read_key('GROQ_API_KEY', c) == 'gsk_from_file' and read_key('OTHER', c) == 'quoted'
    monkeypatch.setenv('GROQ_API_KEY', 'gsk_from_env')
    assert read_key('GROQ_API_KEY', c) == 'gsk_from_env'
    assert read_key('MISSING', c) == ''
    c.writer_chain = ['groq:openai/gpt-oss-120b', 'openrouter:some/model:free#NOT_SET_ANYWHERE']
    links = [s for s, _ in ChainWriter(c).links]
    assert links == ['groq:openai/gpt-oss-120b']                     # a writer without a key is left out


# ---------------------------------------------------------------- the starting context (SessionStart)
def big_service(cfg, monkeypatch):
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    svc = Service(cfg)
    g = svc.mem(GLOBAL_SPACE)
    for i in range(20):
        g.pin(f'Rule {i}: the user wants replies as short numbered points and plain English, case number {i} ' + 'x' * 60)
    m = svc.mem('proj', 'coding')
    for i in range(9):
        m.pin(f'OUTDATED: old note number {i} about a task that ended long ago')
    m.pin('The release branch is release_2026_11')
    m.pin('The  release branch is   RELEASE_2026_11')                 # the same pin twice
    for i in range(40):
        m.store.upsert_topic(f't{i}', name=f'topic_{i}', desc='d', small='A summary sentence about this topic. ' * 6)
    m.store.note('wake_note', '\n'.join(f'- unfinished item number {i} with some detail' for i in range(80)))
    return svc


def test_wake_puts_the_users_rules_first_and_stays_small(cfg, monkeypatch):
    svc = big_service(cfg, monkeypatch)
    ctx = svc.handle('/wake', {'space': 'proj', 'source': 'startup'})['context']
    assert len(ctx) <= WAKE_MAX_CHARS
    head = ctx[:2000]
    assert '[things the user told me to remember in every project]' in head and 'Rule 19:' in head
    assert 'OUTDATED' not in ctx.upper()
    assert ctx.lower().count('release branch is') == 1
    assert '[how your memory works' in ctx and '[memory topics]' in ctx


def test_wake_after_compact_keeps_the_pack_whole(cfg, monkeypatch):
    svc = big_service(cfg, monkeypatch)
    for i in range(30):
        svc.handle('/log', {'space': 'proj', 'role': 'agent', 'session': 'chat1',
                            'content': f'The staging server number_{i} for project_falcon listens on port {8000 + i} ' + 'y' * 200})
    svc.handle('/compacted', {'space': 'proj', 'session': 'chat1', 'wait': True})
    ctx = svc.handle('/wake', {'space': 'proj', 'session': 'chat1', 'source': 'compact'})['context']
    assert len(ctx) <= WAKE_MAX_CHARS and 'Rule 19:' in ctx[:2000]
    assert 'trust the newer one that has evidence' in ctx and "[this chat's facts from memory" in ctx
    pack = ctx.split("[this chat's facts from memory, newest first]\n")[1].split('\n\n')[0].splitlines()
    assert 0 < len(pack) <= 20 and all(len(l) <= 160 + 20 for l in pack)


def test_hook_caps_the_starting_context(monkeypatch):
    from hypnos import hooks
    monkeypatch.setattr(hooks, 'ensure_running', lambda wait=0: True)
    monkeypatch.setattr(hooks, 'call', lambda path, d, timeout=0: {'context': 'z' * 20000})
    got = []
    hooks.run('start', {'session_id': 's', 'cwd': '.'}, got.append)
    assert len(got[0]) == 9000


# ---------------------------------------------------------------- other chats
def test_other_chats_are_one_line_without_their_words(cfg, monkeypatch):
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    svc = Service(cfg)
    secret = 'Please draft the confidential offer letter for Zanzibar_Holdings'
    svc.handle('/log', {'space': 'proj', 'session': 'A', 'content': secret})
    r = svc.handle('/recall', {'space': 'proj', 'session': 'B', 'query': 'how do I run the unit tests here?'})
    assert r['others'] == ['1 other chat is open in this project; its task is not yours.']
    blob = json.dumps(r)
    assert 'Zanzibar' not in blob and 'offer letter' not in blob
    assert svc.handle('/recall', {'space': 'solo', 'session': 'B', 'query': 'anything at all here'})['others'] == []
