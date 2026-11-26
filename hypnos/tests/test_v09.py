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


def test_review_cleanup_forget_purge(cfg):
    mem = Memory('test', 'chat', cfg=cfg, writer=FakeWriter())
    mem._restated = lambda *a: False          # duplicates stored before the no-duplicates rule must still be cleanable
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
    # v0.9.10: one line with the number of other chats, never their words
    assert r['others'] == ['2 other chats are open in this project; their tasks are not yours.']
    assert not any('payment_gateway' in o or 'login_test' in o or 'refactor' in o for o in r['others'])
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


def test_coding_memory_skips_restatements_but_keeps_new_numbers(mem):
    mem.log('The scheduled task JackMicAutoSwitch switches the default capture device', role='agent', session='s1')
    mem.sleep()
    mem.log('The scheduled task JackMicAutoSwitch switches the default capture device on plug', role='agent', session='s2')
    mem.log('Service svc_a listens on port 8081 in cluster_east', role='agent', session='s2')
    mem.sleep()
    mem.log('Service svc_a listens on port 8082 in cluster_east', role='agent', session='s3')
    mem.sleep()
    claims = [r['claim'] for r in mem.store.q("SELECT claim FROM facts WHERE status='active'")]
    assert sum('JackMicAutoSwitch' in c for c in claims) == 1
    assert any('8082' in c for c in mem.store.q_claims()) if hasattr(mem.store, 'q_claims') else         any('8082' in r['claim'] for r in mem.store.q("SELECT claim FROM facts"))


def test_housekeeping_commands_are_not_logged(tmp_path, monkeypatch):
    from hypnos import hooks
    monkeypatch.setenv('HYPNOS_HOME', str(tmp_path))
    logged = []
    monkeypatch.setattr(hooks, 'call', lambda path, payload=None, timeout=0: logged.append((path, payload)) or {})
    monkeypatch.setattr(hooks, 'ensure_running', lambda wait=0: True)
    ev = {'session_id': 'sess-q', 'cwd': str(tmp_path)}
    hooks.run('prompt', {**ev, 'prompt': '/memory-cleanup'}, print)
    hooks.run('tool', {**ev, 'tool_name': 'mcp__hypnos__memory_cleanup_apply', 'tool_input': {'selection': '1-5'}}, print)
    hooks.run('prompt', {**ev, 'prompt': '1-5, T2'}, print)
    hooks.run('tool', {**ev, 'tool_name': 'Bash', 'tool_input': {'command': 'ls'}, 'tool_response': 'x'}, print)
    hooks.run('reply', {**ev, 'last_assistant_message': 'Removed 5 facts, 112 remain.'}, print)
    hooks.run('prompt', {**ev, 'prompt': '/deleate'}, print)
    assert not [p for p, _ in logged if p == '/log']
    hooks.run('prompt', {**ev, 'prompt': 'Now please fix the login page redirect bug in the auth module of the web app'}, print)
    assert [p for p, _ in logged if p == '/log']                    # normal work is remembered again


def test_repeat_in_other_words_is_not_stored_without_system_one(mem, monkeypatch):
    monkeypatch.setattr(mem.vec, 'similarity', lambda text, others: [0.97 for _ in others])
    mem.log('Deleted Claude sessions are sent to the Recycle_Bin instead of being erased', role='agent', session='s1')
    mem.sleep()
    mem.log('Deleted Claude sessions go to the Recycle_Bin rather than being erased for good', role='agent', session='s2')
    mem.sleep()
    assert sum('Recycle' in r['claim'] for r in mem.store.q("SELECT claim FROM facts WHERE status='active'")) == 1


def test_system_one_account_problem_is_reported(mem, monkeypatch):
    from hypnos import jev as J
    monkeypatch.setitem(J.DOWN, 'reason', 'System One refused with HTTP 402: no credits')
    mem.log('The deploy target is staging_east', role='agent', session='s1')
    mem.sleep()
    assert 'HTTP 402' in mem.wake() and mem.status()['systemone_problem']


def test_coding_memory_drops_junk_without_system_one(mem):
    mem.log('Wrote a one-off probe to tmp_scratch/probe_42.py and ran it once', role='agent', session='s1')
    mem.log('The production database lives at db_host_prod:5432 behind the vpn_gateway', role='agent', session='s1')
    mem.sleep()
    claims = [r['claim'] for r in mem.store.q("SELECT claim FROM facts WHERE status='active'")]
    assert any('db_host_prod' in c for c in claims) and not any('tmp_scratch' in c for c in claims)


def test_user_stated_facts_are_never_dropped_as_junk(mem):
    mem.pin('Always keep tmp_scratch/notes.md, I use it daily')
    assert any('tmp_scratch' in r['claim'] for r in mem.store.q("SELECT claim FROM facts WHERE status='active'"))


def test_per_message_memory_never_repeats_until_compaction(mem):
    ctx = ('[topic deploy / small] staging deploys\n\n[facts]\n- (2026-10-01) staging port is 8443  evidence: "8443"\n'
           '- (2026-10-02) bucket is s3://stage  evidence: "stage"\n\n[original messages]\n[2026-10-01 log:3] use 8443\nsecond line')
    first = mem.only_new('s1', ctx)
    assert first == ctx
    again = ctx.replace('- (2026-10-02) bucket is s3://stage  evidence: "stage"', '- (2026-10-03) region is eu  evidence: "eu"')
    second = mem.only_new('s1', again)
    assert second == '[facts]\n- (2026-10-03) region is eu  evidence: "eu"'      # only what it hasn't seen yet
    assert mem.only_new('s2', ctx) == ctx                                          # another session gets everything
    mem.log('hi', 'user', 's1')
    mem.mark_compacted('s1')                                                       # compaction: it forgot, give it again
    assert mem.only_new('s1', ctx) == ctx


def test_service_reloads_changed_config(tmp_path, monkeypatch):
    import os
    import time
    monkeypatch.setenv('HYPNOS_HOME', str(tmp_path))
    (tmp_path / 'config.toml').write_text('prompt_tokens = 800\n', encoding='utf-8')
    svc = Service()
    assert svc.cfg.prompt_tokens == 800
    (tmp_path / 'config.toml').write_text('prompt_tokens = 300\n', encoding='utf-8')
    os.utime(tmp_path / 'config.toml', (time.time() + 5, time.time() + 5))
    svc.handle('/status', {'space': 'x'})
    assert svc.cfg.prompt_tokens == 300


def test_long_chat_notice_once_per_step(cfg):
    svc = Service(cfg)
    m = svc.mem('x', 'coding')
    assert svc.long_chat_note(m, 's1', 55) == ''                                  # below the 60% default
    assert '65%' in svc.long_chat_note(m, 's1', 65)
    assert svc.long_chat_note(m, 's1', 68) == ''                                  # same 10% step: said already
    assert '72%' in svc.long_chat_note(m, 's1', 72)
    assert '/compact' in svc.long_chat_note(m, 's2', 80) and 'Do not suggest /clear' in svc.long_chat_note(m, 's3', 80)


def test_context_tokens_from_transcript(tmp_path):
    from hypnos import hooks
    import json
    t = tmp_path / 't.jsonl'
    t.write_text('\n'.join(json.dumps(x) for x in [
        {'type': 'user', 'message': {'content': 'hi'}},
        {'type': 'assistant', 'message': {'usage': {'input_tokens': 10, 'cache_read_input_tokens': 120000,
                                                     'cache_creation_input_tokens': 500}}}]), encoding='utf-8')
    assert hooks._context_tokens(str(t)) == 120510
    assert hooks._context_tokens(None) == 0
    assert hooks._context_window('no-such-session', str(t)) == 0             # unknown window: no notice, never a wrong one


def test_config_with_bom_still_loads(tmp_path, monkeypatch):
    from hypnos.config import load_config
    monkeypatch.setenv('HYPNOS_HOME', str(tmp_path))
    (tmp_path / 'config.toml').write_bytes(b'\xef\xbb\xbfprompt_tokens = 500\n')   # what PowerShell's utf8 writes
    assert load_config().prompt_tokens == 500


def test_user_rules_are_shared_with_every_project(cfg, monkeypatch):
    """v0.9.7: a rule the user states in one folder's chat reaches chats started in any other folder."""
    import json

    class RuleWriter(FakeWriter):
        def complete(self, prompt, attachments=()):
            if 'Return ONLY a JSON array' in prompt and 'everywhere' in prompt:
                return json.dumps([
                    {'claim': 'The user wants every email draft shown before it is sent', 'kind': 'convention',
                     'quote': 'always show me the email draft before sending', 'entities': ['email'], 'everywhere': True},
                    {'claim': 'The site build uses the folder dist_site', 'kind': 'convention',
                     'quote': 'always show me the email draft before sending', 'entities': ['dist_site'], 'everywhere': True}])
            return super().complete(prompt, attachments)

    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: RuleWriter())
    svc = Service(cfg)
    svc.handle('/log', {'space': 'code-site', 'profile': 'coding', 'session': 's1', 'role': 'user',
                        'content': 'always show me the email draft before sending'})
    svc.handle('/sleep', {'space': 'code-site', 'profile': 'coding', 'session': 's1', 'wait': True})
    shared = [f['claim'] for f in svc.mem('global').store.q("SELECT claim FROM facts WHERE status='active'")]
    assert any('email draft' in c for c in shared)
    # a brand-new folder with no memory of its own still gets the rule when it is relevant
    r = svc.handle('/recall', {'space': 'code-other', 'profile': 'coding', 'session': 's2',
                               'query': 'write the email draft to the client'})
    assert 'email draft' in r['context']
    # sharing the same rule again does not store it twice
    assert svc.share('The user wants every email draft shown before it is sent') is None


def test_inferred_facts_and_missing_paths_are_labelled(cfg, tmp_path):
    """v0.9.8: a fact the writer concluded is marked, and a file it names that is gone now is flagged (plain code)."""
    import json

    gone = tmp_path / 'build_output_v3.zip'

    class OriginWriter(FakeWriter):
        def complete(self, prompt, attachments=()):
            if '"origin"' in prompt and 'Return ONLY a JSON array' in prompt:
                return json.dumps([{'claim': f'The release archive is {gone}', 'kind': 'outcome',
                                    'quote': 'release archive written', 'entities': ['release archive'], 'origin': 'inferred'}])
            return super().complete(prompt, attachments)

    mem = Memory('t', 'coding', cfg=cfg, writer=OriginWriter())
    mem.log('release archive written', role='agent', session='old')
    mem.sleep()
    f = mem.store.q("SELECT * FROM facts WHERE status='active'")[0]
    assert json.loads(f['meta'])['origin'] == 'inferred'
    flags = mem.fact_flags(f)
    assert 'inferred, not stated' in flags and 'not on this machine now' in flags
    gone.write_text('x')
    assert 'not on this machine' not in mem.fact_flags(f)


def test_import_reads_claude_chats_and_text_files(cfg, tmp_path, monkeypatch):
    import json
    from hypnos import importer
    monkeypatch.setattr('hypnos.importer.load_config', lambda: cfg)
    chat = tmp_path / 'abc-123.jsonl'
    lines = [{'type': 'user', 'sessionId': 'abc-123', 'cwd': str(tmp_path), 'timestamp': '2026-10-01T10:00:00Z',
              'message': {'role': 'user', 'content': 'We deploy the site with deploy.mjs through the REST API'}},
             {'type': 'assistant', 'sessionId': 'abc-123', 'timestamp': '2026-10-01T10:00:05Z',
              'message': {'content': [{'type': 'thinking', 'thinking': 'x'}, {'type': 'text', 'text': 'Noted, REST API only.'}]}},
             {'type': 'user', 'isMeta': True, 'message': {'content': '<system-reminder>hidden</system-reminder>'}},
             {'type': 'user', 'message': {'content': 'Long-term memory (hypnos, space "x") ...'}}]
    chat.write_text('\n'.join(json.dumps(x) for x in lines), encoding='utf-8')
    note = tmp_path / 'notes.md'
    note.write_text('# Notes\n\nThe backup drive is E:.\n\nDeploys go through the REST API.', encoding='utf-8')
    session, cwd, turns = importer.claude_chat(chat)
    assert session == 'abc-123' and [t[0] for t in turns] == ['user', 'agent'] and turns[0][2].startswith('2026-10-01')
    got = importer.plan([tmp_path], space='code-x')
    kinds = sorted(k for k, *_ in got)
    assert kinds == ['chat', 'file'] and all(sp == 'code-x' for _, _, sp, _, _ in got)
    assert importer.run([tmp_path], space='code-x', dry=True) == 0


def test_path_check_has_no_false_alarms(mem, tmp_path):
    """Real false alarms from the first day: a trailing comma, a command named without .exe, another user's home."""
    import os
    from pathlib import Path
    tool = tmp_path / ('tool.exe' if os.name == 'nt' else 'tool')
    tool.write_text('x')
    assert mem.missing_paths(f'the tool is at {tool}, and runs fine') == []
    if os.name == 'nt':
        assert mem.missing_paths(f'the tool is at {tmp_path / "tool"}') == []
    other = Path.home().parent / 'someone_else_xyz'
    assert mem.missing_paths(f'on the other laptop the user is {other}') == []
    assert mem.missing_paths(f'old output {tmp_path / "gone_file.txt"} was used') != []


def test_restart_resumes_unsorted_sessions(cfg, monkeypatch):
    """v0.9.8: after a restart, sessions with unsorted messages are queued again (the queue itself is in memory)."""
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    svc = Service(cfg)
    svc.handle('/log', {'space': 'code-a', 'profile': 'coding', 'session': 'old-chat', 'content': 'The port is 8443 for staging'})
    svc2 = Service(cfg)                                   # a fresh service, as after a restart
    m = svc2.mem('code-a', 'coding')
    svc2.jobs = __import__('queue').Queue()
    svc2.queued.clear()
    svc2.resume('code-a', m)
    assert ('code-a', 'old-chat') in svc2.queued


# ---------------------------------------------------------------- v0.9.8 review fixes: import and supersede

def _two_facts(cfg, monkeypatch, stored_ts, new_ts):
    """A live fact (stored_ts) and, afterwards, an imported one (new_ts) about the same thing; System One says
    'newer_replaces' for any pair. Returns (memory, labels the judge was shown)."""
    m = Memory('test', 'coding', cfg=cfg, writer=FakeWriter())
    shown = []

    def choice(state, instructions, options, role='judge'):
        if 'older' not in state:                                       # e.g. the topic choice: no answer
            return None
        shown.append(state)
        return 'newer_replaces'
    monkeypatch.setattr(m.jev, 'choice', choice)
    m.log('The deploy target is staging_east for the site', role='agent', session='live', meta={'ts': stored_ts})
    m.sleep()
    m.log('The deploy target is staging_west for the site', role='agent', session='imp', meta={'ts': new_ts, 'imported': 'x.jsonl'})
    m.sleep()
    return m, shown


def test_imported_older_fact_never_replaces_a_newer_one(cfg, monkeypatch):
    m, shown = _two_facts(cfg, monkeypatch, '2026-11-10T10:00:00Z', '2026-09-01T10:00:00Z')
    assert shown and shown[0]['older']['date'] == '2026-09-01' and shown[0]['newer']['date'] == '2026-11-10'
    assert 'staging_west' in shown[0]['older']['claim']
    rows = {r['claim']: r for r in m.store.q('SELECT * FROM facts')}
    nov = next(r for c, r in rows.items() if 'staging_east' in c)
    sep = next(r for c, r in rows.items() if 'staging_west' in c)
    assert nov['status'] == 'active' and nov['valid_to'] is None          # the November fact is untouched
    assert sep['status'] == 'superseded' and sep['superseded_by'] == nov['id']   # September is kept as history
    assert sep['valid_to'] == nov['valid_from']


def test_newer_fact_still_replaces_an_older_one(cfg, monkeypatch):
    m, shown = _two_facts(cfg, monkeypatch, '2026-09-01T10:00:00Z', '2026-11-10T10:00:00Z')
    assert shown[0]['older']['date'] == '2026-09-01' and shown[0]['newer']['date'] == '2026-11-10'
    rows = {r['claim']: r for r in m.store.q('SELECT * FROM facts')}
    assert next(r for c, r in rows.items() if 'staging_east' in c)['status'] == 'superseded'
    assert next(r for c, r in rows.items() if 'staging_west' in c)['status'] == 'active'


def _chat_file(path, session, cwd, n=2):
    import json
    lines = [{'type': 'user' if k % 2 == 0 else 'assistant', 'sessionId': session, 'cwd': str(cwd),
              'timestamp': f'2026-10-0{k + 1}T10:00:00Z',
              'message': {'content': f'Message number {k} says the build uses folder dist_site{k}'}} for k in range(n)]
    path.write_text('\n'.join(json.dumps(x) for x in lines), encoding='utf-8')
    return path


@pytest.fixture
def imp(cfg, tmp_path, monkeypatch):
    """The importer wired to an in-process service (no network). `fail` lists /log call numbers that get no answer."""
    from hypnos import importer
    monkeypatch.setattr('hypnos.importer.load_config', lambda: cfg)
    monkeypatch.setattr('hypnos.client.load_config', lambda: cfg)
    monkeypatch.setattr('hypnos.importer.ensure_running', lambda: True)
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    svc = Service(cfg)
    state = {'fail': set(), 'paused': False, 'logs': 0}

    def fake_call(path, payload=None, timeout=10.0):
        if path == '/log':
            state['logs'] += 1
            if state['logs'] in state['fail']:
                return None
            if state['paused']:
                return {'id': None, 'paused': True}
        return svc.handle(path, payload or {})
    monkeypatch.setattr('hypnos.importer.call', fake_call)
    return importer, svc, state


def _rows(svc, space, session):
    return svc.mem(space, 'coding').store.q('SELECT content, meta FROM log WHERE session=? ORDER BY id', (session,))


def test_import_is_not_marked_done_when_a_message_fails_and_a_rerun_finishes_it(imp, tmp_path):
    importer, svc, state = imp
    chat = _chat_file(tmp_path / 'c1.jsonl', 'sess-1', tmp_path, n=3)
    state['fail'] = {2}                                               # the second message gets no answer
    assert importer.run([chat], space='code-x') == 1
    assert importer._load_done() == {}                                # not marked: the next run retries
    assert len(_rows(svc, 'code-x', 'sess-1')) == 1
    assert importer._seen_live('code-x', 'sess-1') is False           # its own import rows are not "seen live"
    assert importer.run([chat], space='code-x') == 0                  # rerun: only the missing two, no duplicates
    assert [r['content'] for r in _rows(svc, 'code-x', 'sess-1')] == [f'Message number {k} says the build uses folder dist_site{k}' for k in range(3)]
    assert list(importer._load_done()) == [str(chat.resolve())]


def test_import_into_a_paused_memory_is_not_marked_done(imp, tmp_path):
    importer, svc, state = imp
    chat = _chat_file(tmp_path / 'c2.jsonl', 'sess-2', tmp_path)
    state['paused'] = True
    assert importer.run([chat], space='code-x') == 1
    assert importer._load_done() == {} and _rows(svc, 'code-x', 'sess-2') == []


def test_chat_continued_after_import_gets_only_its_new_turns(imp, tmp_path):
    importer, svc, state = imp
    chat = _chat_file(tmp_path / 'c3.jsonl', 'sess-3', tmp_path, n=2)
    assert importer.run([chat], space='code-x') == 0
    _chat_file(tmp_path / 'c3.jsonl', 'sess-3', tmp_path, n=4)        # the chat went on (file changed)
    import os
    os.utime(chat, (chat.stat().st_atime, chat.stat().st_mtime + 10))
    assert importer.run([chat], space='code-x') == 0
    assert len(_rows(svc, 'code-x', 'sess-3')) == 4
    # a chat Hypnos saw live is still left alone
    svc.handle('/log', {'space': 'code-x', 'profile': 'coding', 'session': 'sess-live', 'content': 'The live chat talks about dist_live'})
    live = _chat_file(tmp_path / 'c4.jsonl', 'sess-live', tmp_path)
    assert importer.run([live], space='code-x') == 0
    assert len(_rows(svc, 'code-x', 'sess-live')) == 1                # nothing was added to it


def test_imported_messages_do_not_make_a_live_session(cfg, monkeypatch):
    monkeypatch.setattr('hypnos.engine.make_writer', lambda c: FakeWriter())
    svc = Service(cfg)
    svc.handle('/log', {'space': 'code-a', 'profile': 'coding', 'session': 'old', 'role': 'user',
                        'content': 'Please change the staging port to 8443 in the config', 'meta': {'imported': 'old.jsonl'}})
    assert ('code-a', 'old') not in svc.seen and svc.live_sessions('code-a') == set()
    assert svc.mem('code-a', 'coding').store.note('task:old') in (None, '')
    svc.handle('/log', {'space': 'code-a', 'profile': 'coding', 'session': 'now', 'role': 'user',
                        'content': 'Please change the staging port to 9000 in the config'})
    assert ('code-a', 'now') in svc.seen and 'staging port' in svc.mem('code-a', 'coding').store.note('task:now')
    assert svc.others_now(svc.mem('code-a', 'coding'), 'code-a', 'x') == ['1 other chat is open in this project; its task is not yours.']


def test_missing_folder_uses_the_repository_space(cfg, tmp_path, monkeypatch):
    from hypnos import importer
    monkeypatch.setattr('hypnos.client.load_config', lambda: cfg)
    (tmp_path / 'myrepo' / '.git').mkdir(parents=True)
    gone = tmp_path / 'myrepo' / 'web' / 'deleted_sub'                # a subfolder that no longer exists
    assert importer._space_for_chat('s-a', str(gone)) == 'code-myrepo'
    assert importer._space_for_chat('s-b', str(tmp_path / 'norepo' / 'gone_dir')) == 'code-gone_dir'
    pins = cfg.home / 'session-spaces'
    pins.mkdir(parents=True, exist_ok=True)
    (pins / 's-c.txt').write_text('code-pinned', encoding='utf-8')    # what the hooks recorded when the chat ran
    assert importer._space_for_chat('s-c', str(gone)) == 'code-pinned'


def test_mcp_answers_version_probe_at_once_and_passes_other_lines():
    # v0.9.8 fix (23 Nov): clients (opencode 2.x, Claude Code) send a version probe first and wait only 5 s. Hypnos must answer it
    # before anything slow loads, and pass every other message to the real MCP server unchanged.
    import json
    import subprocess
    import sys
    code = ('import sys; from hypnos.mcp_server import _answer_probes_early; _answer_probes_early()\n'
            'for line in sys.stdin: sys.stdout.write("echo " + line); sys.stdout.flush()')
    p = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    probe = {'jsonrpc': '2.0', 'id': 'server-discover-probe-1', 'method': 'server/discover', 'params': {}}
    init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}
    out, _ = p.communicate((json.dumps(probe) + '\n' + json.dumps(init) + '\n').encode(), timeout=60)
    lines = out.decode().splitlines()
    reply = json.loads(lines[0])
    assert reply['id'] == 'server-discover-probe-1' and reply['error']['code'] == -32601
    assert lines[1] == 'echo ' + json.dumps(init)                       # the handshake reached the server untouched
    assert len(lines) == 2                                             # the probe itself was not passed on
