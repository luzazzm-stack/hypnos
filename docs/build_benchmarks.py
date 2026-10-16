"""Builds docs/benchmarks.html: every benchmark report and result on one page.

Reuses the style, icon sprite and theme switch of docs/architecture.html, the "Test results" section of that
page (own-sessions test and the agentmem-vs-keyword LoCoMo run), and the head-to-head report written by
benchmark/locomo/compare/compare_systems.py (agentmem vs Mem0 vs GBrain vs keyword), if it exists.
    python docs/build_benchmarks.py
"""
import html
import json
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
arch = (DOCS / 'architecture.html').read_text(encoding='utf-8')
RDIR = ROOT / 'benchmark' / 'locomo' / 'compare' / 'results'
REPORT = next((RDIR / f for f in ('report_v08.json', 'report_v07.json', 'report.json') if (RDIR / f).exists()), RDIR / 'report.json')

head = arch[:arch.index('</style>')]
head = re.sub(r'<title>.*?</title>', '<title>Agent Memory Benchmarks</title>', head, flags=re.S)
head = re.sub(r'<meta name="description" content=".*?">',
              '<meta name="description" content="All benchmark reports and results for the agent memory architecture: '
              'own coding sessions, LoCoMo, and a head-to-head against Mem0 and GBrain.">', head, flags=re.S)
EXTRA_CSS = """
.sys-grid { display: grid; gap: 12px; margin-top: 24px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
.sys { border: 1px solid var(--line); border-radius: 18px; padding: 18px 20px; background: var(--panel); }
.sys.win { border-color: var(--green-line); background: var(--green-bg); }
.sys .n { font-size: 14px; color: var(--muted); }
.sys .v { font-size: 40px; font-weight: 700; letter-spacing: -0.03em; line-height: 1.1; margin-top: 6px; }
.sys.win .v { color: var(--green); }
.sys .d { font-size: 13px; color: var(--muted); margin-top: 6px; }
.bar-fill.me { background: var(--green); }
.pending { border: 1px dashed var(--line-strong); border-radius: 18px; padding: 20px; margin-top: 24px; color: var(--muted); }
"""
sprite = arch[arch.index('<!-- icon sprite -->'):arch.index('</svg>\n\n<div class="top">') + 6]
top = arch[arch.index('<div class="top">'):arch.index('<div class="wrap" id="top">')]
top = re.sub(r'<nav class="nav".*?</nav>', '<nav class="nav" aria-label="Contents">'
             '<a href="#summary">Summary</a><a href="#opus">Opus 5.5</a><a href="#h2h">Muse Spark</a><a href="#rust">Rust vs Python</a><a href="#own">Coding sessions</a>'
             '<a href="#locomo-1">LoCoMo v0.6</a><a href="#images">Images</a><a href="#method">Method</a>'
             '<a href="architecture.html">Architecture</a></nav>', top, flags=re.S)
theme_js = re.search(r"/\* theme \*/.*?\}\);\n", arch, re.S).group(0)

def block(name):
    """A results block of the architecture page, between its <!-- bench:NAME:start/end --> markers, without its heading."""
    t = arch[arch.index(f'<!-- bench:{name}:start -->'):arch.index(f'<!-- bench:{name}:end -->')]
    return t[t.index('</h3>') + 5:]


own = block('own').replace('in section 10', '')
for cut in ('<h3>What it showed at the time (v0.6)</h3>', '<h3>What it shows</h3>'):   # results only, no interpretation
    if cut in own:
        own = own[:own.index(cut)]
own = ('<p class="sec-intro">The first test: 60 questions from 15 real Claude Code sessions, run with versions v0.3 to v0.6 '
       '(25–26 September 2026). It has not been rerun with v0.8 yet.</p>') + own
loc = block('locomo06')
if '<div class="facts">' in loc:
    loc = loc[:loc.index('<div class="facts">')]


def section(sid, num, icon, title, pill, inner, badge='n'):
    return (f'<section class="block" id="{sid}" aria-labelledby="h-{sid}">\n  <div class="sec-head"><span class="badge-ic {badge}">'
            f'<svg class="ic"><use href="#{icon}"/></svg></span><div><div class="num">{num:02d}</div><h2 id="h-{sid}">{title}'
            f'{f" <span class=\"pill g\" style=\"vertical-align:middle\">{pill}</span>" if pill else ""}</h2></div></div>\n{inner}\n</section>\n')


def bars(label, rows, aria):
    out = [f'<div class="result-card"><div class="rc-head"><b>{label[0]}</b><span>{label[1]}</span></div>'
           f'<div class="bars" role="img" aria-label="{html.escape(aria)}">']
    for name, v, me in rows:
        out.append(f'<div class="bar-row"><span class="bl">{name}</span><span class="bar-track"><span class="bar-fill{" me" if me else ""}" '
                   f'style="width:{v}%"></span></span><span class="bv">{v:g}%</span></div>')
    return ''.join(out) + '</div></div>'


NAMES = {'agentmem_v08': 'My architecture v0.8', 'agentmem_v07': 'My architecture v0.7', 'agentmem': 'My architecture v0.6', 'mem0': 'Mem0', 'gbrain': 'GBrain', 'keyword': 'Keyword search'}
if REPORT.exists():
    rep = json.loads(REPORT.read_text(encoding='utf-8'))
    acc = rep['accuracy']; order = sorted(acc, key=lambda s: -acc[s]); best = order[0]
    cards = ''.join(f'<div class="sys{" win" if s == best else ""}"><div class="n">{NAMES[s]}</div><div class="v">{acc[s]:g}%</div>'
                    f'<div class="d">~{rep["ctx_tokens"][s]:,} tokens of context · "Not mentioned" {rep["not_mentioned"][s]}×</div></div>'
                    for s in order)
    cats = ['multi-hop', 'temporal', 'single-hop', 'open-domain']
    rows = ''.join(f'<tr><td>{NAMES[s]}</td>' + ''.join(
        f'<td>{"<b>" if rep["by_category"][s][c] == max(rep["by_category"][x][c] for x in order) else ""}{rep["by_category"][s][c]:g}%'
        f'{"</b>" if rep["by_category"][s][c] == max(rep["by_category"][x][c] for x in order) else ""}</td>' for c in cats) +
        f'<td><span class="val">{acc[s]:g}%</span></td></tr>' for s in order)
    h2h_inner = (
        '<p class="sec-intro">The same 200 LoCoMo questions, now with two popular memory systems added. Every system gets the same '
        'answering model (Muse Spark 1.3), the same answer prompt and about the same context budget (~4,000 tokens). Claude Haiku '
        'grades all six answers to each question side by side (v0.6, v0.7 and v0.8 of my architecture, Mem0, GBrain, keyword search), without knowing which system wrote which.</p>'
        f'<div class="sys-grid">{cards}</div>'
        '<div class="result-grid">' +
        bars(('Overall accuracy', '200 questions'), [(NAMES[s], acc[s], s.startswith('agentmem')) for s in order],
             'Overall accuracy: ' + ', '.join(f'{NAMES[s]} {acc[s]} percent' for s in order)) +
        bars(('Multi-hop: combining facts', '50 questions'),
             [(NAMES[s], rep['by_category'][s]['multi-hop'], s.startswith('agentmem')) for s in sorted(order, key=lambda s: -rep['by_category'][s]['multi-hop'])],
             'Multi-hop accuracy: ' + ', '.join(f'{NAMES[s]} {rep["by_category"][s]["multi-hop"]} percent' for s in order)) +
        '</div><h3>By question type</h3><div class="scroll"><table><thead><tr><th>System</th><th>Multi-hop</th><th>Temporal</th>'
        f'<th>Single-hop</th><th>Open-domain</th><th>Overall</th></tr></thead><tbody>{rows}</tbody></table></div>'
        '<h3>How each system was set up</h3><ul class="narrow">'
        '<li><b>My architecture v0.6, v0.7 and v0.8</b>: agentmem, the same stored memories (sleep consolidation, Jev decisions, bge-small vectors). v0.7: evidence-first hybrid recall. v0.8: Jev-routed recall: Jev picks the question type, the summary level that answers, and each fact or message that is part of the answer.</li>'
        '<li><b>Mem0</b>: mem0ai 2.2 open source, its own fact extraction (Muse as its LLM), bge-small embeddings, local Qdrant, '
        'with spaCy extras; one add per chat session.</li>'
        '<li><b>GBrain</b>: gbrain 0.59, PGLite, hybrid search (keyword + vector + RRF) with bge-small, one page per chat session. '
        'Its reranker and LLM query expansion were off, because both need paid APIs.</li>'
        '<li><b>Keyword search</b>: the top 10 raw turns by keyword match.</li></ul>'
        '')
    h2h_pill = '200 questions · 6 systems'
    summary_h2h = (f'<div class="fact"><span class="badge-ic g"><svg class="ic"><use href="#i-flag"/></svg></span><div><b>Answered by Muse Spark 1.3 (small free model)</b>{" · ".join(f"{NAMES[s]} {acc[s]:g}%" for s in order)}.</div></div>')
else:
    h2h_inner = ('<p class="sec-intro">The same 200 LoCoMo questions, with Mem0 and GBrain added under identical conditions '
                 '(same answering model, same prompt, same ~4,000-token budget, graded side by side by Claude Haiku).</p>'
                 '<div class="pending">Run in progress: results will appear here when it finishes.</div>')
    h2h_pill = 'running'
    summary_h2h = ('<div class="fact"><span class="badge-ic y"><svg class="ic"><use href="#i-refresh"/></svg></span><div><b>Head-to-head '
                   '(LoCoMo)</b>Mem0 and GBrain on the same 200 questions: running now.</div></div>')


RUST = RDIR / 'report_rust.json'
if RUST.exists():
    rr = json.loads(RUST.read_text(encoding='utf-8'))
    ra, rt, rn = rr['accuracy'], rr['ctx_tokens'], rr['not_mentioned']
    rs, ps = rr['recall_seconds_rust'], rr['recall_seconds_python']
    rust_inner = (
        '<p class="sec-intro">The same 200 LoCoMo questions with the Rust engine (agentmem-rs, kept for the future) and the Python engine (the main engine), both running '
        'architecture v0.8 on the same stored memories. For every question both engines recalled while warm and were timed; '
        'answers came from the same model (Muse Spark 1.3) and Claude Haiku graded all answers side by side.</p>'
        f'<div class="sys-grid"><div class="sys win"><div class="n">With Rust</div><div class="v">{ra["agentmem_v08_rust"]:g}%</div>'
        f'<div class="d">~{rt["agentmem_v08_rust"]:,} tokens · median search {rs["median"]:g} s</div></div>'
        f'<div class="sys"><div class="n">Without Rust (Python)</div><div class="v">{ra["agentmem_v08"]:g}%</div>'
        f'<div class="d">~{rt["agentmem_v08"]:,} tokens · median search {ps["median"]:g} s</div></div></div>'
        '<div class="scroll"><table><thead><tr><th></th><th>With Rust</th><th>Without Rust (Python)</th></tr></thead><tbody>'
        f'<tr><td>Accuracy (200 questions)</td><td><span class="val">{ra["agentmem_v08_rust"]:g}%</span></td><td>{ra["agentmem_v08"]:g}%</td></tr>'
        f'<tr><td>Context per question</td><td><b>~{rt["agentmem_v08_rust"]:,} tokens</b></td><td>~{rt["agentmem_v08"]:,} tokens</td></tr>'
        f'<tr><td>"Not mentioned" answers</td><td><b>{rn["agentmem_v08_rust"]}</b></td><td>{rn["agentmem_v08"]}</td></tr>'
        f'<tr><td>Search time, median</td><td><b>{rs["median"]:g} s</b></td><td>{ps["median"]:g} s</td></tr>'
        f'<tr><td>Search time, average</td><td><b>{rs["mean"]:g} s</b></td><td>{ps["mean"]:g} s</td></tr>'
        f'<tr><td>Search time, slowest 10%</td><td><b>{rs["p90"]:g} s</b></td><td>{ps["p90"]:g} s</td></tr>'
        '<tr><td>Search time, cold start (process start + model load)</td><td><b>~3.9 s</b></td><td>~7.8 s</td></tr>'
        f'<tr><td>Peak RAM</td><td>~{max(x["rust_service_peak_mb"] for x in rr["ram_mb"])} MB (service)</td><td>~{max(x["python_process_peak_mb"] for x in rr["ram_mb"])} MB (process)</td></tr>'
        '</tbody></table></div>'
        '<h3>By question type</h3><div class="scroll"><table><thead><tr><th></th><th>Multi-hop</th><th>Temporal</th><th>Single-hop</th><th>Open-domain</th></tr></thead><tbody>'
        + ''.join(f'<tr><td>{label}</td>' + ''.join(f'<td>{rr["by_category"][key][c]:g}%</td>' for c in ('multi-hop', 'temporal', 'single-hop', 'open-domain')) + '</tr>'
                  for label, key in (('With Rust', 'agentmem_v08_rust'), ('Without Rust (Python)', 'agentmem_v08')))
        + '</tbody></table></div>')
else:
    rust_inner = ''

OPUS = RDIR / 'report_opus.json'
if OPUS.exists():
    ro = json.loads(OPUS.read_text(encoding='utf-8'))
    oa, ot, onm, ou = ro['accuracy'], ro['ctx_tokens'], ro['not_mentioned'], ro['opus_usage']
    order_o = sorted(oa, key=lambda s: -oa[s])
    ocards = ''.join(f'<div class="sys{" win" if s == order_o[0] else ""}"><div class="n">{NAMES[s]}</div><div class="v">{oa[s]:g}%</div>'
                     f'<div class="d">~{ot[s]:,} tokens of context · "Not mentioned" {onm[s]}×</div></div>' for s in order_o)
    orows = ''.join(f'<tr><td>{NAMES[s]}</td>' + ''.join(f'<td>{ro["by_category"][s][c]:g}%</td>' for c in ('multi-hop', 'temporal', 'single-hop', 'open-domain'))
                    + f'<td><span class="val">{oa[s]:g}%</span></td></tr>' for s in order_o)
    opus_inner = (
        '<p class="sec-intro">The same 200 LoCoMo questions answered by <b>Claude Opus 5.5</b> (medium effort) instead of the small free model, '
        'so scores sit on the same scale as published results. Each system context was rebuilt from the same memories; <b>Claude Haiku 4.5</b> '
        'graded all four answers to each question blind and side by side.</p>'
        f'<div class="sys-grid">{ocards}</div><div class="result-grid">'
        + bars(('Accuracy', 'answered by Claude Opus 5.5'), [(NAMES[s], oa[s], s.startswith('agentmem')) for s in order_o],
               'Accuracy with Claude Opus 5.5: ' + ', '.join(f'{NAMES[s]} {oa[s]} percent' for s in order_o))
        + bars(('Multi-hop: combining facts', 'answered by Claude Opus 5.5'),
               [(NAMES[s], ro['by_category'][s]['multi-hop'], s.startswith('agentmem')) for s in sorted(order_o, key=lambda s: -ro['by_category'][s]['multi-hop'])],
               'Multi-hop accuracy with Claude Opus 5.5')
        + '</div><h3>By question type</h3><div class="scroll"><table><thead><tr><th>System</th><th>Multi-hop</th><th>Temporal</th><th>Single-hop</th>'
        f'<th>Open-domain</th><th>Overall</th></tr></thead><tbody>{orows}</tbody></table></div>'
        f'<p class="narrow muted">Run cost: {ou["calls"]} Claude Opus 5.5 calls, {(ou["input_tokens"] + ou["output_tokens"]) / 1e6:.2f}M tokens '
        f'(${ou["usd_list_price"]:g} at list price).</p>')
    summary_opus = (f'<div class="fact"><span class="badge-ic g"><svg class="ic"><use href="#i-flag"/></svg></span><div><b>Latest: answered by Claude Opus 5.5</b>'
                    f'{" · ".join(f"{NAMES[s]} {oa[s]:g}%" for s in order_o)} (LoCoMo, 200 questions, graded by Claude Haiku 4.5).</div></div>')
else:
    opus_inner, summary_opus = '', ''

summary = (
    '<div class="facts">'
    + summary_opus + summary_h2h +
    '<div class="fact"><span class="badge-ic g"><svg class="ic"><use href="#i-zap"/></svg></span><div><b>v0.8: 55% with ~600 tokens</b>'
    'LoCoMo 41% (v0.6) → 53% (v0.7) → 55% (v0.8) on the same stored memories. v0.8 sends ~600 tokens per question: 5× less than v0.7, about half of Mem0.</div></div>'
    + ('<div class="fact"><span class="badge-ic g"><svg class="ic"><use href="#i-cpu"/></svg></span><div><b>Rust engine</b>'
       f'Same accuracy as Python ({ra["agentmem_v08_rust"]:g}%), ~{rt["agentmem_v08_rust"]:,} tokens of context, median search {rs["median"]:g} s vs {ps["median"]:g} s.</div></div>' if rust_inner else '') +
    '<div class="fact"><span class="badge-ic g"><svg class="ic"><use href="#i-code"/></svg></span><div><b>Own coding sessions (v0.6)</b>'
    '96% on 60 questions (keyword search 90%), about 10× fewer tokens than plain opencode.</div></div>'
    '</div>')

others = """<p class="sec-intro">Other memory systems for AI agents, and how they differ from this architecture.</p>
<div class="scroll"><table><thead><tr><th>System</th><th>What it does</th><th>Compared with this architecture</th></tr></thead><tbody>
<tr><td><b>Letta</b> (MemGPT)</td><td>The agent edits a small core memory and searches archival and recall stores with tools, like an operating system</td><td>Closest to the layers here, but the agent decides what to keep; here sleep consolidation does</td></tr>
<tr><td><b>Zep / Graphiti</b></td><td>A temporal knowledge graph: every fact has a valid-from and valid-to time</td><td>Strong on changing facts and dates, like the validity windows here; no small/medium/big ladder</td></tr>
<tr><td><b>Mem0</b></td><td>Extracts facts from each message and adds or updates them in a vector store</td><td>Tested above. Flat facts, no hierarchy</td></tr>
<tr><td><b>GBrain</b></td><td>Markdown pages with hybrid search, a typed graph, citations and corrections</td><td>Tested above. The strongest search engine, no hierarchy</td></tr>
<tr><td><b>LangMem</b></td><td>Memory tools for LangGraph agents: facts, episodes and learned instructions</td><td>A toolkit, not a full architecture</td></tr>
<tr><td><b>Cognee</b></td><td>Turns documents into a knowledge graph plus vectors</td><td>Aimed at documents more than chat or coding sessions</td></tr>
<tr><td><b>Supermemory</b></td><td>A hosted memory API for apps and chat</td><td>A product, mostly a black box</td></tr>
<tr><td><b>A-MEM</b> (research)</td><td>Zettelkasten-style linked notes that evolve as new memories arrive</td><td>Similar to the entity links here</td></tr>
<tr><td><b>MemoryOS / MemOS</b> (research)</td><td>Short-, mid- and long-term memory tiers, managed like an operating system</td><td>The closest match to the layered design</td></tr>
<tr><td><b>HippoRAG 2</b> (research)</td><td>Hippocampus-inspired memory: a graph plus PageRank-style retrieval</td><td>Excellent at multi-hop, the weak spot here</td></tr>
</tbody></table></div>
<p class="narrow muted">What is unique here: a small → medium → big → original-log ladder, a cheap judge (Jev) that decides how deep to go, and sleep-time consolidation. Several systems search better: GBrain, HippoRAG and Zep.</p>"""

method = """<ul class="narrow">
<li><b>Answering model</b>: Claude Opus 5.5 (medium effort) for the latest run; Muse Spark 1.3 through stock opencode for the others. Always the same model for every system in a test.</li>
<li><b>Grader</b>: Claude Haiku 4.5 for every LoCoMo run.</li>
<li><b>Grading</b>: own-sessions test, Muse Spark against exact quotes; LoCoMo, Claude Haiku (a different model family), with all answers to a question graded side by side and anonymised.</li>
<li><b>LoCoMo</b> (Maharana et al., 2024): 10 long two-person chats, 200 questions, 50 in each of 4 categories; the adversarial category is excluded, as in most published results.</li>
<li>Head-to-head runs were split over two laptops on different internet connections and merged; every question is graded with all systems' answers together.</li>
<li>Every model call is cached, so each test can be rerun or extended cheaply. Harnesses: <code>benchmark/sessions</code>, <code>benchmark/locomo</code>, <code>benchmark/locomo/compare</code> (<code>run_v07.py</code>, <code>run_v08.py</code>, <code>run_rust.py</code>) in the agent-memory-architecture repo.</li>
</ul>"""

images = """<p class="sec-intro">Share-ready images of these results (in the <code>images</code> folder next to this page).</p>
<div class="result-grid">
<a class="result-card" href="images/benchmark-opus-v0.8-vs-mem0-gbrain-1080x1920.png"><div class="rc-head"><b>Answered by Claude Opus 5.5</b><span>latest · story size</span></div><img src="images/benchmark-opus-v0.8-vs-mem0-gbrain-1080x1920.png" alt="LoCoMo with Claude Opus 5.5 answering" style="width:100%;border-radius:12px;margin-top:12px"></a>
<a class="result-card" href="images/benchmark-v0.8-vs-mem0-gbrain-1080x1920.png"><div class="rc-head"><b>Answered by Muse Spark 1.3</b><span>story size</span></div><img src="images/benchmark-v0.8-vs-mem0-gbrain-1080x1920.png" alt="LoCoMo head-to-head chart" style="width:100%;border-radius:12px;margin-top:12px"></a>
</div>
<p class="narrow muted">Earlier images (v0.6, coding sessions, the first LoCoMo run) are in <code>images/old</code>.</p>"""

page = (head + EXTRA_CSS + '</style>\n</head>\n<body>\n\n' + sprite + '\n\n' + top +
        '<div class="wrap" id="top">\n\n<header class="hero" style="grid-template-columns:1fr"><div>'
        '<h1>Benchmark results</h1><p class="sub">Latest (29 September 2026): answered by Claude Opus 5.5, v0.8 scores 73% on LoCoMo with ~610 tokens of context, '
        'ahead of Mem0 (70%) and GBrain (67%). Answered by Muse Spark 1.3 (small free model): v0.8 55%, Mem0 51%, GBrain 51%. Earlier tests (own coding sessions, LoCoMo v0.6) follow below.</p></div></header>\n' +
        section('summary', 1, 'i-grid', 'At a glance', '', summary) +
        (section('opus', 2, 'i-flag', 'Latest: answered by Claude Opus 5.5', '200 questions · 4 systems', opus_inner, 'g') if opus_inner else '') +
        section('h2h', 3, 'i-flag', 'v0.8 vs Mem0 and GBrain, answered by Muse Spark 1.3', h2h_pill, h2h_inner, 'g') +
        (section('rust', 4, 'i-zap', 'Rust engine vs Python engine (answered by Muse Spark 1.3)', '200 questions · same memories', rust_inner, 'g') if rust_inner else '') +
        section('own', 5, 'i-code', 'Earlier: own coding sessions (answered and graded by Muse Spark 1.3)', '60 questions · v0.3 to v0.6', own, 'n') +
        section('locomo-1', 6, 'i-search', 'Earlier: LoCoMo v0.6 vs keyword search (answered by Muse Spark 1.3)', '200 questions', loc, 'n') +
        section('images', 7, 'i-eye', 'Images', '', images) +
        section('method', 8, 'i-sliders', 'How the tests were run', '', method) +
        '<footer>Benchmark report, updated 29 September 2026 (v0.8). Architecture: <a href="architecture.html">architecture.html</a>.<br><br><b>© 2026 Luza Maharjan. All rights reserved.</b> May not be copied, republished or presented as someone else’s work without written permission.</footer>\n</div>\n'
        '<script>\n(function () {\n  var root = document.documentElement;\n' + theme_js + '})();\n</script>\n</body>\n</html>\n')
(DOCS / 'benchmarks.html').write_text(page, encoding='utf-8')
print('wrote', DOCS / 'benchmarks.html', len(page), 'chars; head-to-head:', 'yes' if REPORT.exists() else 'pending')
