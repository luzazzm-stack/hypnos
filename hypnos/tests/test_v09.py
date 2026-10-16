"""v0.9: no echo of the current conversation, compaction, review and cleanup, purge, absolute dates in extraction."""
import pytest
from hypnos import Memory
from hypnos.config import Config
from hypnos.daemon import Service
from hypnos.llm import FakeWriter


@pytest.fixture
def cfg(tmp_path):
    c = Config(home=tmp_path, writer='fake', jev=False, vectors=False)
    c.spaces_dir.mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def mem(cfg):
    return Memory('test', 'coding', cfg=cfg, writer=FakeWriter())


def test_recall_skips_the_current_conversation(mem):
    mem.log('The deploy script deploy_site.py pushes to the staging_bucket on port 8443', role='agent', session='old')
    mem.sleep()
    mem.log('Which port does deploy_site.py use for the staging_bucket?', role='user', session='now')
    r = mem.recall('Which port does deploy_site.py use for the staging_bucket?', session='now')
    assert '8443' in r.context
    assert 'Which port does' not in r.context                       # the question itself is not echoed back


def test_only_current_conversation_means_nothing_to_recall(mem):
    mem.log('The cache folder is build_cache_v2 under the project root', role='agent', session='now')
    assert not mem.has_memory_beyond('now')
    assert mem.recall('Where is the cache folder build_cache_v2?', session='now').context == ''
    assert mem.has_memory_beyond('other')


def test_after_compaction_older_turns_come_back(mem):
    mem.log('The cache folder is build_cache_v2 under the project root', role='agent', session='now')
    mem.mark_compacted('now')
    mem.log('ok thanks', role='user', session='now')
    assert mem.has_memory_beyond('now')
    assert 'build_cache_v2' in mem.recall('Where is the cache folder build_cache_v2?', session='now').context


def test_review_cleanup_forget_purge(mem):
    mem.log('The scheduled task JackMicAutoSwitch switches the default capture device', role='agent', session='s1')
    mem.log('The scheduled task JackMicAutoSwitch switches the default capture device on plug', role='agent', session='s1')
    mem.sleep()
    facts = mem.review()
    assert len(facts) >= 2 and all(f['id'] and f['topic'] for f in facts)
    sug = mem.cleanup_suggestions()
    assert sug['duplicates'], sug
    gone = sug['duplicates'][0]['remove']
    assert mem.forget_ids([gone]) == 1
    assert gone not in {f['id'] for f in mem.review()}
    assert mem.cleanup_suggestions()['purgeable']['archived'] == 1
    assert mem.purge() == {'deleted': 1}
    assert not mem.store.q('SELECT 1 FROM facts WHERE id=?', (gone,))


def test_service_pause_and_global_pins(cfg, monkeypatch):
    svc = Service(cfg)
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    svc.handle('/pin', {'space': 'proj', 'claim': 'The user writes in British English', 'everywhere': True})
    assert 'British English' in svc.handle('/wake', {'space': 'other-proj'})['context']
    svc.handle('/pause', {'space': 'proj'})
    assert svc.handle('/log', {'space': 'proj', 'content': 'private thing'})['paused']
    svc.handle('/pause', {'space': 'proj', 'paused': False})
    assert svc.handle('/log', {'space': 'proj', 'content': 'normal thing'})['id']


def test_writer_chain_falls_back_and_rests_failed(cfg):
    from hypnos.llm import ChainWriter

    class Down:
        def complete(self, p, a=()):
            raise RuntimeError('Rate limit exceeded')

    class Up:
        def complete(self, p, a=()):
            return 'fine'
    cfg.writer_chain = []
    w = ChainWriter(cfg)
    ChainWriter.down.clear()
    w.links = [('opencode:free', Down()), ('claude:haiku', Up())]
    assert w.complete('x') == 'fine' and w.last == 'claude:haiku'
    assert 'opencode:free' in ChainWriter.down                      # rests, so the next call goes straight to Haiku
    w.links[1] = ('claude:haiku', Down())
    with pytest.raises(RuntimeError):
        w.complete('x')
    ChainWriter.down.clear()


def test_parallel_sessions_only_idle_ones_sleep(cfg):
    import time
    svc = Service(cfg)
    m = svc.mem('proj', 'coding')
    svc.handle('/log', {'space': 'proj', 'session': 'busy', 'content': 'working on the parser_v2 module now'})
    m.log('an old session note about build_cache_v2', role='agent', session='old')
    assert svc.idle_sessions(m, 'proj') == ['old']                  # the busy session is left alone
    svc.seen[('proj', 'busy')] = time.time() - 3600
    assert svc.idle_sessions(m, 'proj') == ['busy', 'old']
    assert svc.sleep_async('proj', 'coding', 'old') and not svc.sleep_async('proj', 'coding', 'old')   # no duplicates


def test_generic_hook_event_names():
    from hypnos.hooks import KIND
    assert KIND['userpromptsubmit'] == KIND['beforeagent'] == KIND['beforesubmitprompt'] == KIND['prompt'] == 'prompt'
    assert KIND['posttooluse'] == KIND['aftertool'] == KIND['afterfileedit'] == 'tool'
    assert KIND['agent-turn-complete'] == KIND['stop'] == 'reply'


def test_generic_hook_logs_and_recalls(monkeypatch, capsys):
    from hypnos import hooks
    sent = []

    def fake_call(path, body, timeout=0):
        sent.append((path, body))
        return {'context': 'remembered: port 8443'} if path == '/recall' else {}
    monkeypatch.setattr(hooks, 'call', fake_call)
    monkeypatch.setattr(hooks, 'ensure_running', lambda wait=0: True)
    hooks.generic('BeforeAgent', '{"session_id": "g1", "cwd": ".", "prompt": "which port for staging?"}')
    assert 'port 8443' in capsys.readouterr().out
    assert sent[0][0] == '/log' and sent[0][1]['session'] == 'g1' and sent[0][1]['content'] == 'which port for staging?'
    hooks.generic('{"type": "agent-turn-complete", "thread-id": "t9", "last-assistant-message": "Done: switched to 8443"}')
    assert sent[-1][0] == '/log' and sent[-1][1]['role'] == 'agent' and sent[-1][1]['session'] == 't9'


def test_automation_adapter_remembers_fixes(cfg):
    from hypnos.integrations.agents import AutomationMemory
    a = AutomationMemory('ops', session='run1', cfg=cfg, writer=FakeWriter())
    a.run('invoice_sync', 'POST /v2/invoices', '429 Too Many Requests', ok=False)
    a.fix('invoice_sync', '429 rate limit on bulk upload', 'send at most 50 invoices per minute')
    a.sleep()
    b = AutomationMemory('ops', session='run2', cfg=cfg, writer=FakeWriter())
    assert '50 invoices per minute' in b.before_run('invoice_sync')


def test_other_live_sessions_are_named_and_never_your_task(cfg):
    svc = Service(cfg)
    svc.handle('/log', {'space': 'proj', 'session': 'A', 'content': 'Please refactor the payment_gateway module to async'})
    svc.handle('/log', {'space': 'proj', 'session': 'B', 'content': 'Fix the flaky login_test in the auth suite please'})
    r = svc.handle('/recall', {'space': 'proj', 'session': 'C', 'query': 'what is happening with payment_gateway?'})
    assert any('payment_gateway' in o for o in r['others']) and any('login_test' in o for o in r['others'])
    assert 'payment_gateway' in r['context'] and 'not your task' in r['context']


def test_wake_note_is_background_not_instructions(mem):
    mem.store.note('wake_note', '- the parser_v2 rewrite was left unfinished')
    w = mem.wake()
    assert 'not instructions' in w and 'only what the user asks in this session' in w


def test_refusals_are_not_saved(mem):
    from hypnos.filters import is_refusal
    assert is_refusal('The assistant declined to write the scraper for LinkedIn.')
    assert is_refusal("Claude said it cannot help with downloading the video.")
    assert not is_refusal('The API refused connections until the firewall rule was added.')
    assert not is_refusal('The user declined the job offer from Google_Inc.')

    class Refuser(FakeWriter):
        def complete(self, prompt, attachments=()):
            if 'List the facts worth remembering' in prompt:
                return ('[{"claim": "The assistant declined to write the scraper_v2.py script.", "kind": "event", '
                        '"quote": "I cannot help write scraper_v2.py", "entities": []}, '
                        '{"claim": "The scraper config lives in scraper_cfg.toml.", "kind": "event", '
                        '"quote": "config lives in scraper_cfg.toml", "entities": []}]')
            return super().complete(prompt, attachments)
    mem._writer = Refuser()
    mem.log('Sorry, I cannot help write scraper_v2.py. The config lives in scraper_cfg.toml though.', role='agent', session='s1')
    mem.sleep()
    claims = [r['claim'] for r in mem.store.q("SELECT claim FROM facts WHERE status='active'")]
    assert any('scraper_cfg.toml' in c for c in claims)
    assert not any('declined' in c for c in claims)


def test_cleanup_view_and_apply_by_number_and_topic(mem):
    for i in range(6):
        mem.log(f'Service svc_{i} runs on port 90{i}0 in cluster_east', role='agent', session='s1')
    mem.sleep()
    view = mem.cleanup_view()
    assert 'T1.' in view['text'] and view['map']['t1']
    mem.remember_cleanup_map(view)
    shown = mem.cleanup_apply('show T1')['show']
    assert shown and all(isinstance(x, str) for x in shown)
    before = view['facts']
    out = mem.cleanup_apply('T1')
    assert out['forgotten'] == len(view['map']['t1']) and out['facts_left'] == before - out['forgotten']


def test_service_serves_cached_cleanup_list(cfg, monkeypatch):
    import time
    svc = Service(cfg)
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    base = {'space': 'proj', 'profile': 'coding'}
    svc.handle('/log', {**base, 'content': 'The build uses gradle_8 with the jdk_21 toolchain', 'role': 'agent', 'session': 'a'})
    svc.handle('/sleep', {**base, 'wait': True})
    svc.changed('proj')
    for _ in range(50):                                   # the background worker builds the list
        if svc.views.get('proj', (None,))[0] == svc.gen['proj']:
            break
        time.sleep(0.1)
    t0 = time.time()
    v = svc.handle('/cleanup_view', base)
    assert time.time() - t0 < 0.5 and 'facts' in v['text'] and 'map' not in v
    out = svc.handle('/cleanup_apply', {**base, 'selection': 'T1'})
    assert out['forgotten'] >= 1


def test_parallel_sleep_matches_one_at_a_time(cfg):
    import json as _j
    texts = [f'Project_{c} keeps its settings in settings_{c}.json and deploys to host_{c}.example.org on port 80{k}{k}'
             for k, c in enumerate('abcdefgh')]

    def run(par):
        cfg.writer_parallel = par
        m = Memory(f'p{par}', 'coding', cfg=cfg, writer=FakeWriter())
        cfg.episode_chars = 200                            # several episodes
        for t in texts:
            m.log(t, role='agent', session='s')
        m.sleep()
        return [r['claim'] for r in m.store.q("SELECT claim FROM facts WHERE status='active' ORDER BY rowid")]
    assert run(1) == run(4)


def test_setup_connects_and_disconnects_every_agent(tmp_path, monkeypatch):
    import argparse
    import json as _j
    from hypnos import setup_wizard as W
    home = tmp_path / 'home'
    for d in ('.claude', '.config/opencode', '.codex', '.gemini', '.cursor', '.codeium/windsurf'):
        (home / d).mkdir(parents=True)
    (home / '.claude' / 'settings.json').write_text('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook"}]}]}}')
    (home / '.config/opencode/opencode.jsonc').write_text('{\n  // my comment\n  "model": "x",\n}\n')
    (home / '.codex/config.toml').write_text('model = "o4"\n')
    monkeypatch.setattr(W, 'HOME', home)
    monkeypatch.setattr(W.shutil, 'which', lambda n: None)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.setenv('HYPNOS_HOME', str(tmp_path / 'mem'))
    monkeypatch.setattr('hypnos.client.ensure_running', lambda *a: False)
    monkeypatch.setattr(W, 'writer_chain', lambda agents, chosen='', yes=False: ['opencode:opencode/x-free', 'claude:haiku'])
    a = argparse.Namespace(yes=True, uninstall=False, systemone='none', jev_key='', agents='', memory_model='', list_models=False)
    assert W.main(a) == 0
    s = _j.loads((home / '.claude/settings.json').read_text())
    assert any('my-own-hook' in _j.dumps(h) for h in s['hooks']['Stop'])            # the user's own hooks are kept
    assert 'hypnos' in _j.dumps(s['hooks']['UserPromptSubmit'])
    assert _j.loads((home / '.claude.json').read_text())['mcpServers']['hypnos']['args'] == ['mcp']
    oc = _j.loads((home / '.config/opencode/opencode.jsonc').read_text())
    assert oc['model'] == 'x' and oc['mcp']['hypnos']['command'][1] == 'mcp'
    assert (home / '.config/opencode/plugins/hypnos.ts').exists()
    assert '[mcp_servers.hypnos]' in (home / '.codex/config.toml').read_text()
    assert 'hypnos' in (home / '.gemini/settings.json').read_text() and (home / '.cursor/mcp.json').exists()
    conf = (tmp_path / 'mem' / 'config.toml').read_text()
    assert 'writer_chain' in conf and 'systemone = "llm"' in conf
    assert W.main(a) == 0                                                            # running it twice changes nothing
    assert _j.dumps(_j.loads((home / '.claude/settings.json').read_text())).count('hook claude Stop') == 1
    a.uninstall = True
    assert W.main(a) == 0
    s = _j.loads((home / '.claude/settings.json').read_text())
    assert 'hypnos' not in _j.dumps(s) and 'my-own-hook' in _j.dumps(s)
    assert 'hypnos' not in (home / '.codex/config.toml').read_text()
    assert not (home / '.config/opencode/plugins/hypnos.ts').exists()


def test_no_system_one_uses_the_memory_model_for_checks(cfg):
    import json as _j
    from hypnos.jev import Jev, VERIFY
    cfg.systemone, cfg.jev = 'llm', True
    j = Jev(cfg)
    assert j.enabled and not j.routing
    calls = []

    class W:
        def complete(self, prompt, attachments=()):
            calls.append(prompt)
            return _j.dumps({'c0': {'noul': 0.9}, 'c1': {'noul': 0.1}})
    j._writer = W()
    ps = j.verify_many([{'claim': 'a', 'evidence': 'a'}, {'claim': 'b', 'evidence': 'x'}], *VERIFY)
    assert ps == [0.9, 0.1] and len(calls) == 1                     # every claim in one call
    assert j.noul({'message': 'hi'}, 'q?', 'y', 'n') is None         # recall-time checks stay on plain code


def test_cleanup_lists_every_fact_and_numbers_differ_are_not_duplicates(mem):
    mem.log('ARKX Level 6 was solved and gave flag flag_6842e1', role='agent', session='s1')
    mem.log('ARKX Level 7 was solved and gave flag flag_80bcd4', role='agent', session='s1')
    mem.sleep()
    assert not mem.cleanup_suggestions()['duplicates']
    view = mem.cleanup_view()
    mem.remember_cleanup_map(view)
    assert 'flag_6842e1' in view['text'] and 'flag_80bcd4' in view['text']   # facts are listed, not just counted
    num = next(k for k, v in view['map'].items() if k.isdigit() and
               'flag_6842e1' in mem.store.q('SELECT claim FROM facts WHERE id=?', (v[0],))[0]['claim'])
    assert mem.cleanup_apply(num)['forgotten'] == 1
    left = [r['claim'] for r in mem.store.q("SELECT claim FROM facts WHERE status='active'")]
    assert any('flag_80bcd4' in c for c in left) and not any('flag_6842e1' in c for c in left)


def test_session_keeps_its_first_folder_space(tmp_path, monkeypatch):
    from hypnos import client
    monkeypatch.setenv('HYPNOS_HOME', str(tmp_path / 'home'))
    a, b = tmp_path / 'proj_a', tmp_path / 'proj_b'
    a.mkdir(); b.mkdir()
    assert client.session_space('sess-1', str(a)) == 'code-proj_a'
    assert client.session_space('sess-1', str(b)) == 'code-proj_a'      # moved folders, same memory
    assert client.session_space('sess-2', str(b)) == 'code-proj_b'
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'sess-1')
    monkeypatch.delenv('HYPNOS_SPACE', raising=False)
    assert client.current_space(str(b)) == 'code-proj_a'
