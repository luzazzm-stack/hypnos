"""`hypnos setup`: connect the memory to every agent on this machine, pick the System One model and the writer.

Run by the one-line installers (install.sh / install.ps1), or by hand. Works on Windows, macOS and Linux.
Every config file it changes is backed up first (<file>.bak-hypnos), and `hypnos setup --uninstall` undoes it.

Non-interactive (for coding agents installing it): hypnos setup --yes [--systemone jev|laya|none] [--jev-key KEY]
[--agents claude,opencode,...]. The same options can come from environment variables: HYPNOS_SETUP_YES=1,
HYPNOS_SETUP_SYSTEMONE, TYPESAFE_API_KEY, HYPNOS_SETUP_AGENTS."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import load_config
from .proc import background, hidden

HOME = Path.home()
ASSETS = Path(__file__).parent / 'assets'
MARK = '.bak-hypnos'
RULES = """
## Long-term memory (hypnos)
- At the start of a task, call memory_recall with what the task is about.
- When the user states a preference or decision worth keeping, call memory_remember (everywhere=true if it applies to all projects).
- At the end of a task, call memory_note with what was done, decided, learned, or left open.
- Exact values (paths, ports, commands) come from the quoted evidence; re-check anything marked STALE.
"""

WARNING = """\
  This memory works best with a System One model. A System One model answers yes/no and pick-one questions in one
  fast step instead of writing text, so memory decides what to keep and what to recall for a tiny fraction of the
  cost of an LLM. You save a lot of money and tokens with it.
  Note: without a System One model, answers can sometimes be a little more accurate, but memory uses more tokens,
  because the LLM has to do that work instead.

  Options:
    1. Jev (TypeSafe)   hosted API, needs a key from https://typesafe.ai
    2. Laya             open weights, free, runs on this machine (https://laya-ai.com). About 1-3 GB to download.
                        Tip: run Laya locally next to your coding agent, terminal agent, automation or robot, and
                        memory costs you nothing extra.
    3. None             use the memory model you pick below for these checks too (more tokens)
"""


# ---------------------------------------------------------------- small helpers
def say(msg=''):
    print(msg, flush=True)


def ask(question, default='', yes=False):
    if yes or not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        a = input(f'{question} [{default}]: ').strip()
    except EOFError:
        return default
    return a or default


def backup(path: Path):
    if path.exists() and not path.with_name(path.name + MARK).exists():
        shutil.copy2(path, path.with_name(path.name + MARK))


def read_json(path: Path):
    """JSON or JSONC (comments and trailing commas, as opencode and VS Code allow)."""
    if not path.exists():
        return {}
    text = path.read_text(encoding='utf-8-sig')
    try:
        return json.loads(text)
    except ValueError:
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
        text = re.sub(r'(?m)^[ \t]*//[^\n]*$', '', text)
        text = re.sub(r',(\s*[}\]])', r'\1', text)
        return json.loads(text)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def exe():
    """How agents should start hypnos: always its full path (v0.9.6). Hooks and MCP servers are started by the agent,
    often without the shell's PATH (desktop apps, other launchers), and then a bare `hypnos` silently does nothing."""
    found = shutil.which('hypnos')
    if found:
        p = str(Path(found).resolve())
        return f'"{p}"' if ' ' in p else p
    names = ('hypnos.exe', 'hypnos') if os.name == 'nt' else ('hypnos',)
    for d in (Path(sys.argv[0]).resolve().parent, Path(sys.executable).parent, HOME / '.local' / 'bin'):
        for n in names:
            if (d / n).is_file():
                p = str(d / n)
                return f'"{p}"' if ' ' in p else p
    return 'hypnos'


def opencode_dir():
    base = Path(os.environ.get('XDG_CONFIG_HOME') or HOME / '.config')
    return base / 'opencode'


# ---------------------------------------------------------------- agents
OLD_NAMES = ('hypnos', 'agentmem')                  # agentmem = the name before v0.9; its entries are replaced too


def ours_(entry) -> bool:
    s = json.dumps(entry)
    return any(n in s for n in OLD_NAMES)


def detect():
    """Coding agents installed on this machine, in priority order."""
    found = {}
    checks = [
        ('claude', 'Claude Code', shutil.which('claude') or (HOME / '.claude').exists()),
        ('opencode', 'opencode', shutil.which('opencode') or opencode_dir().exists()),
        ('codex', 'Codex CLI', shutil.which('codex') or (HOME / '.codex').exists()),
        ('gemini', 'Gemini CLI', shutil.which('gemini') or (HOME / '.gemini').exists()),
        ('cursor', 'Cursor', (HOME / '.cursor').exists()),
        ('windsurf', 'Windsurf', (HOME / '.codeium' / 'windsurf').exists()),
    ]
    for key, name, ok in checks:
        if ok:
            found[key] = name
    return found


def wire_claude(cmd, remove=False):
    settings = HOME / '.claude' / 'settings.json'
    data = read_json(settings)
    hooks = data.setdefault('hooks', {})
    for event in list(hooks):                                  # drop our old entries first (idempotent)
        hooks[event] = [h for h in hooks[event] if not ours_(h)]
        if not hooks[event]:
            del hooks[event]
    if not remove:
        ours = json.loads((ASSETS / 'claude-hooks.json').read_text(encoding='utf-8'))['hooks']
        for event, entries in ours.items():
            for e in entries:
                for h in e['hooks']:
                    h['command'] = h['command'].replace('hypnos', cmd, 1)
            hooks.setdefault(event, []).extend(entries)
    if not hooks:
        data.pop('hooks', None)
    write_json(settings, data)
    cmd_file = HOME / '.claude' / 'commands' / 'memory-cleanup.md'
    if remove:
        cmd_file.unlink(missing_ok=True)
    else:
        cmd_file.parent.mkdir(parents=True, exist_ok=True)
        cmd_file.write_text((ASSETS / 'claude-memory-cleanup.md').read_text(encoding='utf-8'), encoding='utf-8')
    claude = shutil.which('claude')                            # MCP server, user scope: through Claude's own CLI
    if claude:
        for n in OLD_NAMES:
            subprocess.run([claude, 'mcp', 'remove', '-s', 'user', n], capture_output=True, timeout=60, **hidden())
        if remove:
            return
        r = subprocess.run([claude, 'mcp', 'add', '-s', 'user', 'hypnos', '--', cmd.strip('"'), 'mcp'],
                           capture_output=True, timeout=60, **hidden())
        if r.returncode == 0:
            return
    claude_json = HOME / '.claude.json'
    cj = read_json(claude_json) if claude_json.exists() else {}
    servers = cj.setdefault('mcpServers', {})
    servers.pop('agentmem', None)
    if remove:
        servers.pop('hypnos', None)
    else:
        servers['hypnos'] = {'type': 'stdio', 'command': cmd.strip('"'), 'args': ['mcp'], 'env': {}}
    write_json(claude_json, cj)


def wire_opencode(cmd, remove=False):
    d = opencode_dir()
    conf = next((d / n for n in ('opencode.json', 'opencode.jsonc') if (d / n).exists()), d / 'opencode.json')
    data = read_json(conf)
    mcp = data.setdefault('mcp', {})
    mcp.pop('agentmem', None)
    (d / 'plugins' / 'agentmem.ts').unlink(missing_ok=True)
    if remove:
        mcp.pop('hypnos', None)
    else:
        mcp['hypnos'] = {'type': 'local', 'command': [cmd.strip('"'), 'mcp'], 'enabled': True, 'timeout': 10000}
    if not mcp:
        data.pop('mcp', None)
    data.setdefault('$schema', 'https://opencode.ai/config.json')
    write_json(conf, data)
    for src, dst in (('opencode-plugin.ts', d / 'plugins' / 'hypnos.ts'),
                     ('opencode-memory-cleanup.md', d / 'command' / 'memory-cleanup.md')):
        if remove:
            dst.unlink(missing_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text((ASSETS / src).read_text(encoding='utf-8'), encoding='utf-8')


def _rules(path: Path, remove=False):
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    text = re.sub(r'\n## Long-term memory \((?:hypnos|agentmem)\)\n(?:- .*\n)+', '\n', text)
    if not remove:
        text = text.rstrip('\n') + '\n' + RULES
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(text, encoding='utf-8')


def wire_codex(cmd, remove=False):
    conf = HOME / '.codex' / 'config.toml'
    text = conf.read_text(encoding='utf-8') if conf.exists() else ''
    text = re.sub(r'\n?\[mcp_servers\.(?:hypnos|agentmem)\]\n(?:[^\[\n].*\n?)*', '\n', text)
    if not remove:
        text = text.rstrip('\n') + f'\n\n[mcp_servers.hypnos]\ncommand = {json.dumps(cmd.strip(chr(34)))}\nargs = ["mcp"]\n'
    conf.parent.mkdir(parents=True, exist_ok=True)
    backup(conf)
    conf.write_text(text.lstrip('\n'), encoding='utf-8')
    _rules(HOME / '.codex' / 'AGENTS.md', remove)


def wire_gemini(cmd, remove=False):
    settings = HOME / '.gemini' / 'settings.json'
    data = read_json(settings)
    servers = data.setdefault('mcpServers', {})
    servers.pop('agentmem', None)
    hooks = data.setdefault('hooks', {})
    for event in list(hooks):
        hooks[event] = [h for h in hooks[event] if not ours_(h)]
        if not hooks[event]:
            del hooks[event]
    if remove:
        servers.pop('hypnos', None)
    else:
        servers['hypnos'] = {'command': cmd.strip('"'), 'args': ['mcp']}
        for ev, ours in (('SessionStart', 'start'), ('BeforeAgent', 'prompt'), ('AfterTool', 'tool'),
                         ('AfterAgent', 'reply'), ('PreCompress', 'compact'), ('SessionEnd', 'end')):
            hooks.setdefault(ev, []).append({'hooks': [{'type': 'command', 'command': f'{cmd} hook generic {ours}'}]})
    for k in ('mcpServers', 'hooks'):
        if not data[k]:
            data.pop(k)
    write_json(settings, data)
    _rules(HOME / '.gemini' / 'GEMINI.md', remove)


def wire_mcp_json(path: Path, cmd, remove=False):
    data = read_json(path)
    servers = data.setdefault('mcpServers', {})
    servers.pop('agentmem', None)
    if remove:
        servers.pop('hypnos', None)
    else:
        servers['hypnos'] = {'command': cmd.strip('"'), 'args': ['mcp']}
    write_json(path, data)


WIRES = {
    'claude': wire_claude,
    'opencode': wire_opencode,
    'codex': wire_codex,
    'gemini': wire_gemini,
    'cursor': lambda cmd, remove=False: wire_mcp_json(HOME / '.cursor' / 'mcp.json', cmd, remove),
    'windsurf': lambda cmd, remove=False: wire_mcp_json(HOME / '.codeium' / 'windsurf' / 'mcp_config.json', cmd, remove),
}


# ---------------------------------------------------------------- memory model (writes facts while saving)
# Nothing is fixed to one model name. Setup lists every model this machine can use; the person, or the coding agent
# running the install, picks the best value for money (`--memory-model`). Without a choice, Hypnos ranks them itself:
# free and local first, then small/fast versions (flash, mini, haiku, luna...), big models only as the last resort.
PROVIDERS = [  # (env var holding the key, OpenAI-compatible base URL)
    ('OPENAI_API_KEY', 'https://api.openai.com/v1'),
    ('DEEPSEEK_API_KEY', 'https://api.deepseek.com/v1'),
    ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai'),
    ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1'),
    ('MISTRAL_API_KEY', 'https://api.mistral.ai/v1'),
    ('GROQ_API_KEY', 'https://api.groq.com/openai/v1'),
    ('XAI_API_KEY', 'https://api.x.ai/v1'),
]
SMALL = re.compile(r'luna|haiku|flash|lite|mini|nano|small|lightning|instant|fast|chat|turbo|air|tiny', re.I)
BIG = re.compile(r'opus|ultra|pro\b|max\b|large|big|reason|think|o\d\b|405b|70b|deep-?research', re.I)
NOT_CHAT = re.compile(r'audio|realtime|tts|transcri|whisper|image|dall|embed|moderation|search|vision-only|rerank|ocr', re.I)


def _models(base, key):
    req = urllib.request.Request(base.rstrip('/') + '/models', headers={'Authorization': 'Bearer ' + key} if key else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read()).get('data') or []
    return sorted(data, key=lambda m: -(m.get('created') or 0))           # newest first


def _version(name):
    m = re.search(r'(\d+(?:\.\d+)?)', name)
    return float(m.group(1)) if m else 0.0


def available_models(agents):
    """Every model the memory could write with on this machine: [{spec, name, cost, via}]."""
    out = []
    if 'opencode' in agents and shutil.which('opencode'):
        try:
            r = subprocess.run([shutil.which('opencode'), 'models', 'opencode'], capture_output=True, text=True,
                               timeout=60, stdin=subprocess.DEVNULL, **hidden())
            for line in r.stdout.splitlines():
                name = line.strip()
                if name.startswith('opencode/'):
                    out.append({'spec': 'opencode:' + name, 'name': name, 'via': 'opencode',
                                'cost': 'free' if name.endswith('-free') else 'paid'})
        except Exception:
            pass
    try:
        tags = json.loads(urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2).read()).get('models') or []
        for m in sorted(tags, key=lambda m: m.get('size') or 1e18):
            out.append({'spec': f'openai:{m["name"]}@http://127.0.0.1:11434/v1', 'name': m['name'], 'via': 'Ollama', 'cost': 'local'})
    except Exception:
        pass
    for env, base in PROVIDERS:
        key = os.environ.get(env)
        if not key:
            continue
        try:
            for m in _models(base, key):
                mid = str(m.get('id', ''))
                mid = mid.split('/', 1)[1] if mid.startswith('models/') else mid
                if mid and not NOT_CHAT.search(mid):
                    out.append({'spec': f'openai:{mid}@{base}#{env}', 'name': mid, 'via': env.split('_')[0].title(),
                                'cost': 'free' if mid.endswith(':free') else 'paid'})
        except Exception:
            pass
    if 'claude' in agents and shutil.which('claude'):
        for alias in ('haiku', 'sonnet', 'opus'):
            out.append({'spec': 'claude:' + alias, 'name': f'Claude {alias.title()} (latest)', 'via': 'Claude Code', 'cost': 'plan'})
    if 'codex' in agents and shutil.which('codex'):
        out.append({'spec': 'codex:', 'name': 'Codex default model (or pass codex:<model>, e.g. the latest GPT Luna)',
                    'via': 'Codex CLI', 'cost': 'plan'})
    return out


def rank(models):
    """Best value first: free > local > your plan > paid; small/fast versions before big ones; newest version first."""
    tier = {'free': 0, 'local': 1, 'plan': 2, 'paid': 3}

    def key(m):
        n = m['name']
        size = 0 if SMALL.search(n) else (2 if BIG.search(n) else 1)
        return (tier.get(m['cost'], 4) + (0 if size < 2 else 3), size, -_version(n))
    return sorted(models, key=key)


def suggest(models, n=6):
    """The default chain: up to 4 free/local models, then 1-2 small paid/plan ones, and one bigger last resort."""
    r = rank(models)
    cheap = [m for m in r if m['cost'] in ('free', 'local')][:4]
    small = [m for m in r if m['cost'] in ('plan', 'paid') and SMALL.search(m['name'])][:2]
    big = [m for m in r if m['cost'] in ('plan', 'paid') and not SMALL.search(m['name']) and not BIG.search(m['name'])][:1]
    chain = cheap + small + big
    return [m['spec'] for m in (chain or r)[:n]]


def writer_chain(agents, chosen='', yes=False):
    """The memory model chain: what --memory-model says, else what the person picks, else Hypnos's own ranking."""
    if chosen:
        return [c.strip() for c in chosen.split(',') if c.strip()]
    models = available_models(agents)
    default = suggest(models)
    if yes or not sys.stdin or not sys.stdin.isatty() or not models:
        return default
    ranked = rank(models)
    say('\n  Models this machine can use for memory (best value first):')
    for i, m in enumerate(ranked[:25], 1):
        mark = '*' if m['spec'] in default else ' '
        say(f'   {mark}{i:>2}. {m["name"]}  ({m["cost"]}, {m["via"]})')
    say('  * = suggested. Press Enter to use the suggestion, or type numbers in order of preference (e.g. 3,1,7).')
    a = ask('  Memory models', '', False)
    picks = [ranked[int(x) - 1]['spec'] for x in re.findall(r'\d+', a) if 0 < int(x) <= len(ranked[:25])]
    return picks or default


# ---------------------------------------------------------------- System One
def laya_up(url):
    try:
        req = urllib.request.Request(url, data=json.dumps({'state': {'t': 'ok'}, 'questions': {
            'q': {'type': 'noul', 'instructions': 'Is this a test?'}}}).encode(), method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r:
            return 'answers' in json.loads(r.read())
    except Exception:
        return False


def install_laya():
    """Laya as its own uv tool (CPU build of torch by default), served on 127.0.0.1:8000 by the memory service."""
    uv = shutil.which('uv')
    if not uv:
        say('  uv not found; install Laya by hand:  pip install "laya[serve]"')
        return False
    say('  Installing Laya (this downloads PyTorch, can take a few minutes)...')
    r = subprocess.run([uv, 'tool', 'install', '--python', '3.12', 'laya[serve]'], **hidden())
    return r.returncode == 0 and bool(shutil.which('laya-serve') or (Path(uv).parent / 'laya-serve').exists())


def write_config(values: dict):
    cfg = load_config()
    path = cfg.home / 'config.toml'
    old = path.read_text(encoding='utf-8') if path.exists() else ''
    keep = [ln for ln in old.splitlines() if ln.split('=')[0].strip() not in values]
    for k, v in values.items():
        keep.append(f'{k} = {json.dumps(v)}')
    path.write_text('\n'.join(keep).strip() + '\n', encoding='utf-8')
    return path


# ---------------------------------------------------------------- main
def main(args):
    yes = args.yes or os.environ.get('HYPNOS_SETUP_YES') == '1'
    cmd = exe()
    found = detect()
    want = args.agents or os.environ.get('HYPNOS_SETUP_AGENTS')
    agents = {k: v for k, v in found.items() if not want or k in want.split(',')}

    if getattr(args, 'list_models', False):           # for the coding agent doing the install: pick from this list
        models = available_models(found)
        best = suggest(models)
        print(json.dumps([{**m, 'suggested': m['spec'] in best} for m in rank(models)], indent=1))
        return 0

    if args.uninstall:
        for k in agents:
            try:
                WIRES[k](cmd, remove=True)
                say(f'  removed from {agents[k]}')
            except Exception as ex:
                say(f'  {agents[k]}: could not remove ({ex})')
        say('Memory is disconnected. Your saved memory stays in ~/.hypnos (delete that folder to erase it).')
        return 0

    say('\nHypnos v0.9 (memory architecture) setup\n')
    if not agents:
        say('No coding agent found. Install Claude Code, opencode, Codex, Gemini CLI, Cursor or Windsurf, then run '
            '`hypnos setup` again. For robots and automation, see the README (Python adapters).')
    for k, name in agents.items():
        try:
            WIRES[k](cmd)
            say(f'  connected: {name}')
        except Exception as ex:
            say(f'  {name}: could not connect automatically ({ex}). See integrations/coding-agents/README.md')

    # System One
    choice = (args.systemone or os.environ.get('HYPNOS_SETUP_SYSTEMONE') or '').lower()
    key = args.jev_key or os.environ.get('TYPESAFE_API_KEY', '')
    cfg = load_config()
    if not choice:
        say('\n' + WARNING)
        default = '1' if key or cfg.jev_credentials.exists() else '3'
        choice = {'1': 'jev', '2': 'laya', '3': 'none'}.get(ask('  Choose 1, 2 or 3', default, yes), 'none')
    values = {}
    if choice == 'jev':
        if not key and not cfg.jev_credentials.exists():
            key = ask('  Jev API key (from typesafe.ai)', '', yes)
        if key:
            cfg.jev_credentials.parent.mkdir(parents=True, exist_ok=True)
            cfg.jev_credentials.write_text(f'TYPESAFE_API_KEY={key}\n', encoding='utf-8')
            try:
                os.chmod(cfg.jev_credentials, 0o600)
            except OSError:
                pass
        values.update(systemone='jev', jev=bool(key or cfg.jev_credentials.exists()))
    elif choice == 'laya':
        url = 'http://127.0.0.1:8000/v1/systemone'
        if not laya_up(url) and not (shutil.which('laya-serve') or install_laya()):
            say('  Laya could not be installed; memory runs without a System One model for now.')
            values.update(systemone='laya', jev=False)
        else:
            values.update(systemone='laya', jev=True)
    else:
        values.update(systemone='llm', jev=True)
        say('  No System One model: the memory model chosen below also checks new facts while saving '
            '(a little more accurate, more tokens).')

    chain = writer_chain(found, args.memory_model or os.environ.get('HYPNOS_SETUP_MEMORY_MODEL', ''), yes)
    if chain:
        values.update(writer='auto', writer_chain=chain)
        say('\n  Writer models (cheapest first, used when memory is saved):')
        for c in chain:
            say('    - ' + c.split('#')[0])
    else:
        say('\n  No writer model found (no opencode, Claude Code, Ollama or API key). Facts are kept as raw text until one '
            'is available; set OPENAI_API_KEY / DEEPSEEK_API_KEY or install opencode, then run `hypnos setup` again.')
        values.update(writer='fake')
        if values.get('systemone') == 'llm':
            values['jev'] = False                          # no model at all: plain rules until one is set up
    path = write_config(values)
    say(f'\n  Settings saved: {path}')

    from .client import call, ensure_running
    ok = ensure_running(20)
    say('  Memory service: running in the background (no window)' if ok else '  Memory service: will start with your next agent session')
    say('\nDone. Restart your coding agent. Memory loads by itself; type /memory-cleanup to see and clean what it keeps.')
    return 0
