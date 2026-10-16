"""Writes each LoCoMo conversation as markdown pages (one page per session) for GBrain to import."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_locomo as R

OUT = Path(__file__).resolve().parent / 'gbrain_pages'
data = json.loads((R.HERE / 'locomo10.json').read_text(encoding='utf-8'))
for i, s in enumerate(data):
    d = OUT / f'conv-{i}'
    d.mkdir(parents=True, exist_ok=True)
    a, b = s['conversation']['speaker_a'], s['conversation']['speaker_b']
    for n, dt, turns in R.sessions(s['conversation']):
        date = (R.parse_date(dt) or '')[:10]
        body = '\n\n'.join(R.turn_text(t) for t in turns)
        (d / f'session-{n:02d}.md').write_text(
            f'---\ntitle: "Conversation between {a} and {b}, session {n} ({dt})"\ntype: note\ndate: {date}\n---\n\n'
            f'# Session {n}: {a} and {b}, {dt}\n\n{body}\n', encoding='utf-8')
print('pages written to', OUT)
