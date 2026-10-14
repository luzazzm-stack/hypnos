"""End-to-end: service + Claude Code hooks + chat proxy + agent adapters, offline (fake writer, no Jev)."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PORT, PROXY_PORT, UP_PORT = 7499, 7498, 7497


@pytest.fixture(scope='module')
def env(tmp_path_factory):
    home = tmp_path_factory.mktemp('hypnos_home')
    e = dict(os.environ, HYPNOS_HOME=str(home), HYPNOS_WRITER='fake', HYPNOS_JEV='0', HYPNOS_PORT=str(PORT),
             HYPNOS_SPACE='code-demo', PYTHONIOENCODING='utf-8')
    e.pop('HYPNOS_DISABLE', None)
    d = subprocess.Popen([sys.executable, '-m', 'hypnos', 'serve'], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(40):
        try:
            urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{PORT}/ping', data=b'{}', method='POST'), timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    yield e
    d.kill()


def post(port, path, payload, headers=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(payload).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def hook(env, event, payload):
    p = subprocess.run([sys.executable, '-m', 'hypnos', 'hook', 'claude', event], input=json.dumps(payload),
                       env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)['hookSpecificOutput']['additionalContext'] if p.stdout.strip() else ''


def test_claude_code_hooks_round_trip(env, tmp_path):
    base = {'session_id': 's1', 'cwd': str(tmp_path)}
    hook(env, 'UserPromptSubmit', {**base, 'prompt': 'set up the default microphone switcher'})
    hook(env, 'PostToolUse', {**base, 'tool_name': 'Bash', 'tool_input': {'command': 'schtasks /create /tn JackMicAutoSwitch'},
                              'tool_response': 'SUCCESS: The scheduled task "JackMicAutoSwitch" has successfully been created.'})
    rep = post(PORT, '/sleep', {'space': 'code-demo', 'profile': 'coding', 'wait': True})
    assert rep['kept'] >= 1
    ctx = hook(env, 'UserPromptSubmit', {**base, 'session_id': 's2', 'prompt': 'What is the name of the scheduled task for the microphone?'})
    assert 'JackMicAutoSwitch' in ctx
    wake = hook(env, 'SessionStart', {**base, 'session_id': 's3', 'source': 'startup'})
    assert 'Long-term memory' in wake


def test_hooks_disabled_inside_writer(env, tmp_path):
    e = dict(env, HYPNOS_DISABLE='1')
    p = subprocess.run([sys.executable, '-m', 'hypnos', 'hook', 'claude', 'SessionStart'], input='{}', env=e,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout == ''


def test_chat_proxy_injects_memory(env):
    seen = {}

    class Up(BaseHTTPRequestHandler):
        def do_POST(self):
            seen['req'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'Noted, you like green tea.'}}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a): pass

    up = ThreadingHTTPServer(('127.0.0.1', UP_PORT), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    post(PORT, '/pin', {'space': 'chat-test', 'profile': 'chat', 'claim': 'The user drinks green tea without sugar every morning'})
    proxy = subprocess.Popen([sys.executable, '-m', 'hypnos', 'chat-proxy', '--upstream', f'http://127.0.0.1:{UP_PORT}/v1',
                              '--port', str(PROXY_PORT), '--space', 'chat-test'], env=env)
    try:
        for _ in range(40):
            try:
                out = post(PROXY_PORT, '/v1/chat/completions', {'model': 'x', 'messages': [
                    {'role': 'user', 'content': 'What do I usually drink in the morning, green tea or coffee?'}]})
                break
            except Exception:
                time.sleep(0.25)
        assert out['choices'][0]['message']['content'].startswith('Noted')
        sys_msgs = [m for m in seen['req']['messages'] if m['role'] == 'system']
        assert sys_msgs and 'green tea' in sys_msgs[0]['content']
    finally:
        proxy.kill(); up.shutdown()


def test_agent_adapters_in_process(tmp_path):
    from hypnos.config import Config
    from hypnos.integrations.agents import RobotMemory, SearchMemory
    from hypnos.llm import FakeWriter
    cfg = Config(home=tmp_path, writer='fake', jev=False)
    cfg.spaces_dir.mkdir(parents=True, exist_ok=True)
    robot = RobotMemory('robot-arm1', cfg=cfg, writer=FakeWriter())
    robot.observe('red_mug detected on kitchen_table_2 at x=1.20 y=0.45', sensor='cam_front')
    robot.act('grasp red_mug', 'gripper closed at 38mm', success=True)
    robot.sleep()
    assert 'kitchen_table_2' in robot.where_is('red_mug')
    search = SearchMemory('research-x', cfg=cfg, writer=FakeWriter())
    search.result('https://example.org/specs', 'Specs', 'The Vega 8 GPU has 512 shader units at 1100 MHz.')
    search.sleep()
    assert 'example.org' in search.recall('How many shader units does the Vega 8 have?')
