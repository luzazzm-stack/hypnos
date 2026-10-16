"""hypnos: layered agent memory that sleeps and shows its evidence (architecture v0.6).

    from hypnos import Memory
    mem = Memory(space="my-project", profile="coding")
    mem.log("ran `pytest -q`, 3 tests failed in test_auth.py", role="tool", session="s1")
    mem.sleep()                                  # consolidate: facts, topics, wake note
    print(mem.recall("which tests failed?").context)
"""
from .config import Config, load_config
from .engine import Memory, Recall
from .profiles import PROFILES

__all__ = ['Memory', 'Recall', 'Config', 'load_config', 'PROFILES']
__version__ = '0.9.5'
