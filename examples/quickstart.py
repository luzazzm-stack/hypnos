"""Quickstart: log a few events, sleep, recall.

    uv run --with-editable ../hypnos python quickstart.py            # offline demo writer, no keys
    HYPNOS_WRITER=opencode uv run --with-editable ../hypnos python quickstart.py   # real LLM writer
"""
import os
import tempfile

from hypnos import Memory
from hypnos.config import Config, load_config

cfg = load_config()
if os.environ.get('HYPNOS_WRITER') is None:          # default to the offline demo writer and a throwaway folder
    cfg = Config(home=tempfile.mkdtemp(prefix='hypnos-demo-'), writer='fake', jev=False)
    cfg.spaces_dir.mkdir(parents=True, exist_ok=True)

mem = Memory('demo', 'coding', cfg=cfg)
mem.log('Please make the login tests pass', role='user', session='s1')
mem.log('Ran pytest -q tests/test_auth.py: 3 failed, token expiry checked before decode', role='tool', session='s1')
mem.log('Fixed validate_jwt in src/auth.py to decode first, then check expiry; all tests pass', role='agent', session='s1')
mem.log('I prefer small, focused commits', role='user', session='s1')   # stored as a fact right away

print('status:', mem.status())
print('sleep :', mem.sleep())
print('\n--- wake context for the next session ---\n' + mem.wake())
r = mem.recall('Which file had the JWT expiry bug and what was the fix?')
print(f'\n--- recall (level={r.level}, exact={r.exact}, ~{r.tokens} tokens) ---\n' + r.context)
