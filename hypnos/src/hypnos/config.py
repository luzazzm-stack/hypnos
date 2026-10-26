"""Settings: ~/.hypnos/config.toml, overridable by HYPNOS_* environment variables."""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(os.environ.get('HYPNOS_HOME', Path.home() / '.hypnos')))
    # writer LLM used during sleep: auto (the chain below) | opencode | claude | openai | fake
    writer: str = 'auto'
    # v0.9: tried in order, cheapest first; a failing writer rests 30 min and the next takes over.
    # Free opencode models first, then small paid Claude (Haiku), then a bigger one only if all else is down.
    writer_chain: list = field(default_factory=lambda: [
        'opencode:opencode/muse-spark-1.3-contributor-free', 'opencode:opencode/nemotron-3.5-lightning-free',
        'opencode:opencode/mimo-v2.6-flash-free', 'opencode:opencode/ling-3.1-flash-free', 'claude:haiku', 'claude:sonnet'])
    writer_model: str = 'opencode/muse-spark-1.3-contributor-free'
    openai_base_url: str = 'http://localhost:11434/v1'   # any OpenAI-compatible server (Ollama, OpenRouter, ...)
    openai_api_key_env: str = 'OPENAI_API_KEY'
    # Jev (TypeSafe) for typed decisions; falls back to heuristics without a key
    jev: bool = True
    # v0.9: the System One decision model: jev (TypeSafe's hosted API, needs a key) or laya (open weights, run
    # locally with `laya-serve`; same /v1/systemone API). systemone_url overrides where requests go.
    systemone: str = 'jev'
    systemone_url: str = ''
    # systemone = "llm": which model answers the checks, as a writer_chain entry such as "claude:haiku";
    # systemone = "laya": the checkpoint name to ask laya-serve for (e.g. one fine-tuned for Hypnos)
    # (empty = the memory model chain itself)
    systemone_model: str = ''
    jev_credentials: Path = field(default_factory=lambda: Path.home() / '.typesafe' / 'credentials')
    gate_min_probability: float = 0.85
    # local meaning-based search (fastembed bge-small); off keeps RAM low
    vectors: bool = False
    # sizes
    episode_chars: int = 12000
    # v0.9: writer calls a sleep runs at once. 0 = automatic: 1 on machines under 12 GB of RAM (each writer is a whole
    # agent process, ~400 MB), 3 above that.
    writer_parallel: int = 0
    topic_small_tokens: int = 60
    topic_medium_tokens: int = 300
    topic_big_tokens: int = 1500
    topic_log_tokens: int = 3000
    jev_parallel: int = 12                     # v0.8: concurrent Jev requests per recall (Jev is free and fast)
    summary_min_probability: float = 0.7       # v0.8: a summary level answers an overview question
    item_min_probability: float = 0.5          # v0.8: a fact or message is part of the answer
    expand_query: bool = False                 # v0.7: ask the writer for related search words before recall (one extra call)
    exact_snippet_tokens: int = 800
    topic_split_facts: int = 24
    anchors_max: int = 30
    wake_tokens: int = 1500
    # v0.9.6: memory added to each user message (was 2500). It stays in the chat, so in a long chat it adds up.
    prompt_tokens: int = 800
    # v0.9.6: once the agent's context passes this size, it is asked to suggest /clear or /compact (0 = never)
    long_chat_tokens: int = 150000
    # daemon
    # v0.9: the service lives only while an agent is open; it stops ~1-2 minutes after the last one closes.
    # true = keep it running (robots, automation, servers).
    stay_running: bool = False
    host: str = '127.0.0.1'
    port: int = 7439

    def __post_init__(self):
        self.home = Path(self.home)                 # accept str paths
        self.jev_credentials = Path(self.jev_credentials)

    @property
    def spaces_dir(self) -> Path:
        return self.home / 'spaces'

    def db_path(self, space: str) -> Path:
        return self.spaces_dir / (safe_space(space) + '.db')


def safe_space(space: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', space).strip('-')[:80] or 'default'


def _migrate_old_home():
    """The memory used to be called agentmem and lived in ~/.agentmem: move it to ~/.hypnos once."""
    old, new = Path.home() / '.agentmem', Path.home() / '.hypnos'
    if old.is_dir() and not new.exists() and not os.environ.get('HYPNOS_HOME'):
        try:
            old.rename(new)
        except OSError:                                   # in use right now: copy instead, the old folder stays
            import shutil
            shutil.copytree(old, new, ignore=shutil.ignore_patterns('*.log', 'opencode-run', 'opencode-cfg'))


def load_config() -> Config:
    _migrate_old_home()
    cfg = Config()
    path = cfg.home / 'config.toml'
    if tomllib and path.exists():
        data = tomllib.loads(path.read_text(encoding='utf-8'))
        for k, v in data.items():
            if hasattr(cfg, k):
                cur = getattr(cfg, k)
                setattr(cfg, k, Path(v) if isinstance(cur, Path) else type(cur)(v))
    for k in list(vars(cfg)):
        env = os.environ.get('HYPNOS_' + k.upper(), os.environ.get('AGENTMEM_' + k.upper()))   # old name still works
        if env is not None:
            cur = getattr(cfg, k)
            if isinstance(cur, bool):
                setattr(cfg, k, env.lower() in ('1', 'true', 'yes', 'on'))
            elif isinstance(cur, Path):
                setattr(cfg, k, Path(env))
            elif isinstance(cur, list):                      # e.g. HYPNOS_WRITER_CHAIN=opencode:a,claude:haiku
                setattr(cfg, k, [x.strip() for x in env.split(',') if x.strip()])
            else:
                setattr(cfg, k, type(cur)(env))
    if cfg.writer_parallel <= 0:
        from .proc import total_ram_gb
        cfg.writer_parallel = 3 if total_ram_gb() >= 12 else 1
    cfg.spaces_dir.mkdir(parents=True, exist_ok=True)
    return cfg
