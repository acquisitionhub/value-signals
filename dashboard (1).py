#!/usr/bin/env python3
"""
Builds docs/index.html: a single self-contained dashboard page from the latest results
(signals, paper portfolio, trade journal, screener + thesis drafts).
GitHub Pages serves the docs/ folder. Run after signals.py / screener.py.
"""
import csv, html, json, os, re
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "output"
DOCS = HERE / "docs"


def read_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def read_csv(p):
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def md_to_html(md):
    """Tiny converter for the thesis drafts (headings, lists, tables, bold, italics)."""
    out, table, in_list = [], [], False

    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<![\w])_(.+?)_(?![\w])", r"<i>\1</i>", t)
        t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        return t

    def flush_table():
        nonlocal table
        if table:
            rows = [r for r in table if not re.match(r"^\|[\s|:-]+\|$", r)]
            h = "<table>"
            for i, r in enumerate(rows):
                cells = [c.strip() for c in r.strip("|").split("|")]
                tag = "th" if i == 0 else "td"
                h += "<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>"
            out.append(h + "</table>")
            table = []

    for line in md.splitlines():
        if line.startswith("|"):
            table.append(line); continue
        flush_table()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline(line[2:])}</li>"); continue
        if in_list:
            out.append("</ul>"); in_list = False
        if line.startswith("## "):
            out.append(f"<h4>{inline(line[3:])}</h4>")
        elif line.startswith("# "):
            out.append(f"<h3>{inline(line[2:])}</h3>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    flush_table()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def gather():
    signals = read_json(OUT / "latest_signals.json", {"stocks": [], "updated": None})
    history = read_csv(OUT / "paper_history.csv")
    positions = read_json(OUT / "paper_positions.json", [])
    journal = read_csv(OUT / "paper_journal.csv")
    screens = sorted(OUT.glob("screen_*.csv"))
    screen = read_csv(screens[-1]) if screens else []
    screen_date = screens[-1].stem.replace("screen_", "") if screens else None
    theses = {}
    for p in sorted((OUT / "theses").glob("*.md")) if (OUT / "theses").exists() else []:
        tk = p.stem.rsplit("_", 1)[0]
        theses[tk] = md_to_html(p.read_text())   # later dates overwrite earlier
    repo = os.environ.get("GITHUB_REPOSITORY")
    scan = read_json(OUT / "scan_summary.json", None)
    wl = HERE / "watchlist.txt"
    watch = [l.strip().upper() for l in wl.read_text().splitlines() if l.strip() and not l.startswith("#")] if wl.exists() else []
    # screener table: passes + your watchlist (a 500-row table would be unreadable)
    screen = [r for r in screen if r.get("passes") == "True" or r.get("ticker") in watch or r.get("source") != "scan"]
    return {"signals": signals, "history": history, "positions": positions,
            "journal": list(reversed(journal))[:200], "screen": screen, "screen_date": screen_date,
            "theses": theses, "repo": repo, "scan": scan}


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Value signals dashboard</title>
<style>
:root{
  --bg:#f6f6f3; --surface-1:#fcfcfb; --surface-2:#f0efea; --border:#e2e1da;
  --text-primary:#1c1c1a; --text-secondary:#55554f; --text-muted:#85847c;
  --series-1:#2a78d6; --series-2:#eb6834; --grid:#e8e7e1;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b; --neutral:#9a998f; --info:#2a78d6;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#121211; --surface-1:#1a1a19; --surface-2:#232321; --border:#33332f;
    --text-primary:#f0efec; --text-secondary:#c3c2b7; --text-muted:#8e8d84;
    --series-1:#3987e5; --series-2:#d95926; --grid:#2c2c29; --neutral:#77766e; --info:#3987e5;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text-primary);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px}
h1{font-size:24px;margin:0;letter-spacing:-.01em}
.sub{color:var(--text-secondary);font-size:13px}
.btn{display:inline-block;padding:7px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-1);color:var(--text-primary);text-decoration:none;font-size:13px}
.btn:hover{background:var(--surface-2)}
nav{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:18px;overflow-x:auto}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:10px 12px;color:var(--text-secondary);font:inherit;cursor:pointer;white-space:nowrap}
nav button[aria-selected=true]{color:var(--text-primary);border-bottom-color:var(--text-primary);font-weight:600}
section{display:none} section.on{display:block}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.tile .k{font-size:12px;color:var(--text-secondary)} .tile .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.chip{border:1px solid var(--border);background:var(--surface-1);color:var(--text-secondary);border-radius:999px;padding:4px 11px;font:inherit;font-size:13px;cursor:pointer}
.chip[aria-pressed=true]{background:var(--text-primary);color:var(--surface-1);border-color:var(--text-primary)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-left:5px solid var(--neutral);border-radius:12px;padding:12px 14px}
.card .top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.card .tk{font-weight:700;font-size:17px} .card .nm{color:var(--text-secondary);font-size:13px}
.badge{font-size:12px;font-weight:650;white-space:nowrap}
.card ul{margin:6px 0 0;padding-left:0;list-style:none;font-size:13px;color:var(--text-secondary)}
.card li{margin:2px 0}
.card .why{color:var(--text-primary);font-size:14px;margin:6px 0}
.val{font-size:13px;color:var(--text-secondary);font-variant-numeric:tabular-nums}
.val b{color:var(--text-primary)}
.opt{font-size:12px;color:var(--text-muted);margin-top:6px}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.panel h2{font-size:16px;margin:0 0 8px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--text-secondary);font-weight:600}
td.n,th.n{text-align:right}
.tablewrap{overflow-x:auto}
.legend{display:flex;gap:16px;font-size:13px;color:var(--text-secondary);margin-bottom:4px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.chart{position:relative}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:6px 9px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.12);display:none;white-space:nowrap}
.empty{color:var(--text-muted);font-size:14px;padding:18px 0}
details{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;margin-bottom:8px}
details summary{cursor:pointer;padding:10px 14px;font-weight:600}
details .body{padding:0 16px 12px}
details .body table{margin:6px 0 10px}
.linkish{background:none;border:0;color:var(--info);cursor:pointer;font:inherit;padding:0;font-size:12px}
a{color:var(--info)}
footer{margin-top:28px;color:var(--text-muted);font-size:12px}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>Value signals</h1><div class="sub" id="updated"></div></div>
  <div id="actions"></div>
</header>
<nav role="tablist">
  <button role="tab" data-tab="signals" aria-selected="true">Signals</button>
  <button role="tab" data-tab="paper" aria-selected="false">Paper portfolio</button>
  <button role="tab" data-tab="journal" aria-selected="false">Trade journal</button>
  <button role="tab" data-tab="screener" aria-selected="false">Screener &amp; theses</button>
</nav>

<section id="signals" class="on">
  <div class="tiles" id="sigTiles"></div>
  <p class="sub" style="margin:-6px 0 12px">Green needs good fundamentals + 3 of 5 daily chart checks. Each ★ adds conviction: ★ the weekly chart agrees, ★ insiders or an activist are buying (SEC filings).</p>
  <div class="chips" id="sigChips"></div>
  <div class="cards" id="sigCards"></div>
</section>

<section id="paper">
  <div class="tiles" id="paperTiles"></div>
  <div class="panel">
    <h2>Paper account vs S&amp;P 500 <span class="sub">(both indexed to 100 at the start)</span></h2>
    <div class="legend" id="legend"></div>
    <div class="chart" id="chart"><div class="tip" id="tip"></div></div>
    <button class="linkish" id="tblToggle">Show as table</button>
    <div class="tablewrap" id="chartTable" hidden></div>
  </div>
  <div class="panel"><h2>Open paper positions</h2><div class="tablewrap" id="positions"></div></div>
</section>

<section id="journal"><div class="panel"><h2>Every paper trade, newest first</h2><div class="tablewrap" id="journalTbl"></div></div></section>

<section id="screener">
  <div class="panel"><h2>Latest screen <span class="sub" id="screenDate"></span></h2><div class="tablewrap" id="screenTbl"></div></div>
  <h2 style="font-size:16px;margin:18px 0 8px">Thesis drafts</h2>
  <div id="theses"></div>
</section>

<footer>Research aid only. Signals line up evidence; they are not predictions or advice. Fundamentals: SEC 10-K filings. Prices: Alpaca / Yahoo Finance. Paper trades use fake money.</footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const usd = (x, d=2) => x == null || isNaN(x) ? 'n/a' : (x < 0 ? '-$' : '$') + Math.abs(+x).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
const pct = x => x == null || x === '' || isNaN(x) ? 'n/a' : ((+x >= 0 ? '+' : '') + (+x*100).toFixed(1) + '%');

// tabs
function showTab(id){
  document.querySelectorAll('nav button').forEach(x => x.setAttribute('aria-selected', x.dataset.tab === id));
  document.querySelectorAll('section').forEach(s => s.classList.toggle('on', s.id === id));
  if (id === 'paper') setTimeout(drawChart, 0);
}
document.querySelectorAll('nav button').forEach(b => b.onclick = () => { history.replaceState(null, '', '#' + b.dataset.tab); showTab(b.dataset.tab); });

// header
const S = D.signals || {stocks: []};
$('#updated').textContent = S.updated ? `Last run ${S.updated} (${S.mode}${S.paper ? ', paper trading on' : ''})` : 'No runs yet';
if (D.repo) $('#actions').innerHTML =
  `<a class="btn" href="https://github.com/${esc(D.repo)}/actions" target="_blank" rel="noopener">Run now / history ↗</a>`;

// ---------- signals ----------
const LIGHT = {
  GREEN: {c:'var(--good)', icon:'●', label:'Green'}, AMBER: {c:'var(--warning)', icon:'◐', label:'Amber'},
  RED: {c:'var(--critical)', icon:'▲', label:'Red'}, GREY: {c:'var(--neutral)', icon:'○', label:'Grey'},
  HOLD: {c:'var(--info)', icon:'■', label:'Hold'}};
const ORDER = {RED:0, GREEN:1, AMBER:2, HOLD:3, GREY:4};
const stocks = [...S.stocks].sort((a,b) => ORDER[a.signal]-ORDER[b.signal] || (b.stars||0)-(a.stars||0) || b.score-a.score);
const isSmart = s => s.smart && s.smart.confirms && s.signal !== 'GREY';
const counts = {}; stocks.forEach(s => counts[s.signal] = (counts[s.signal]||0)+1);

$('#sigTiles').innerHTML = [['Green', counts.GREEN||0], ['Green ★★', stocks.filter(s=>s.stars===2).length],
  ['Smart money buying', stocks.filter(isSmart).length], ['Red', counts.RED||0],
  ['Amber', counts.AMBER||0], ['Held', stocks.filter(s=>s.holding).length]]
  .map(([k,v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
let filter = 'ALL';
const chipDefs = [['ALL','All'], ['GREEN','Green'], ['SMART','Smart money'], ['AMBER','Amber'], ['RED','Red'], ['HOLD','Held'], ['GREY','Grey']];
function renderChips(){
  $('#sigChips').innerHTML = chipDefs.map(([k,l]) => `<button class="chip" data-k="${k}" aria-pressed="${filter===k}">${l}</button>`).join('');
  document.querySelectorAll('#sigChips .chip').forEach(c => c.onclick = () => { filter = c.dataset.k; renderChips(); renderCards(); });
}
function renderCards(){
  const list = stocks.filter(s => filter==='ALL' || (filter==='SMART' ? isSmart(s) : s.signal===filter));
  if (!list.length) { $('#sigCards').innerHTML = '<div class="empty">Nothing here right now.</div>'; return; }
  $('#sigCards').innerHTML = list.map(s => {
    const L = LIGHT[s.signal] || LIGHT.GREY, v = s.value;
    let val = '';
    if (v && v.base != null && v.base <= 0) val = `Value: <b>negative</b> (debt exceeds value) · price <b>${usd(s.price)}</b>`;
    else if (v && v.base != null) val = `Value ${usd(v.bear)} / <b>${usd(v.base)}</b> / ${usd(v.bull)} · price <b>${usd(s.price)}</b>`;
    else val = `Price <b>${usd(s.price)}</b>`;
    const checks = s.checks.map(c => `<li>${c.ok ? '✅' : '❌'} ${esc(c.text)}</li>`).join('') +
      `<li>${s.weekly.confirms ? '✅' : '❌'} <b>weekly:</b> ${esc(s.weekly.text)}</li>` +
      (s.smart ? `<li>${s.smart.confirms ? '✅' : '➖'} <b>smart money:</b> ${esc(s.smart.text)}` +
        (s.smart.activist ? ` <a href="${esc(s.smart.activist.url)}" target="_blank" rel="noopener">filing ↗</a>` : '') + `</li>` : '');
    return `<div class="card" style="border-left-color:${L.c}">
      <div class="top"><div><span class="tk">${esc(s.ticker)}</span> <span class="nm">${esc(s.name)}</span></div>
      <span class="badge"><span style="color:${L.c}">${L.icon}</span> ${L.label}${s.stars ? ' ' + '★'.repeat(s.stars) : ''}${s.holding ? ' · held' : ''}${s.source === 'scan' ? ' · found by scan' : ''}</span></div>
      <div class="val">${val} · ${s.score}/5 daily</div>
      <div class="why">${s.why.join('<br>')}</div>
      <ul>${checks}</ul>
      ${s.options ? `<div class="opt">${esc(s.options)}</div>` : ''}
    </div>`;
  }).join('');
}
if (stocks.length) { renderChips(); renderCards(); }
else $('#sigCards').innerHTML = '<div class="empty">No signals yet. They appear after the first scheduled run.</div>';

// ---------- paper ----------
const H = (D.history || []).map(r => ({d: r.date, eq: +r.equity, spy: r.spy ? +r.spy : null})).filter(r => r.eq);
const base = H[0], last = H[H.length-1];
if (H.length) {
  const myRet = last.eq/base.eq - 1, spyRet = base.spy && last.spy ? last.spy/base.spy - 1 : null;
  $('#paperTiles').innerHTML = [['Account value', usd(last.eq, 0)], ['Return since ' + base.d, pct(myRet)],
    ['S&P 500, same period', pct(spyRet)], ['Ahead / behind', spyRet == null ? 'n/a' : pct(myRet - spyRet)],
    ['Open positions', (D.positions||[]).length]]
    .map(([k,v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
} else $('#paperTiles').innerHTML = '<div class="empty">Paper trading hasn\'t run yet (add your Alpaca keys and run with --paper).</div>';

function drawChart(){
  const box = $('#chart'); box.querySelectorAll('svg').forEach(e => e.remove());
  if (H.length < 2) { if (H.length) box.insertAdjacentHTML('afterbegin','<div class="empty">The chart appears after the second run.</div>'); return; }
  const series = [
    {name:'Paper account', color:'var(--series-1)', vals: H.map(r => r.eq/base.eq*100)},
    {name:'S&P 500', color:'var(--series-2)', vals: H.map(r => r.spy && base.spy ? r.spy/base.spy*100 : null)}];
  $('#legend').innerHTML = series.map(s => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join('');
  const W = Math.max(320, box.clientWidth), Ht = 260, m = {l:44, r:92, t:10, b:26};
  const all = series.flatMap(s => s.vals).filter(v => v != null);
  let lo = Math.min(...all, 100), hi = Math.max(...all, 100); const pad = Math.max(1, (hi-lo)*0.12); lo -= pad; hi += pad;
  const x = i => m.l + (W-m.l-m.r) * (H.length===1 ? 0 : i/(H.length-1));
  const y = v => m.t + (Ht-m.t-m.b) * (1 - (v-lo)/(hi-lo));
  const ticks = 4, step = (hi-lo)/ticks;
  let g = '';
  for (let i=0;i<=ticks;i++){ const v = lo + step*i;
    g += `<line x1="${m.l}" x2="${W-m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>`+
         `<text x="${m.l-8}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="var(--text-muted)">${v.toFixed(0)}</text>`; }
  g += `<line x1="${m.l}" x2="${W-m.r}" y1="${y(100)}" y2="${y(100)}" stroke="var(--text-muted)" stroke-dasharray="3 3"/>`;
  const idx = [0, Math.floor((H.length-1)/2), H.length-1].filter((v,i,a) => a.indexOf(v)===i);
  idx.forEach(i => g += `<text x="${x(i)}" y="${Ht-6}" text-anchor="${i===0?'start':i===H.length-1?'end':'middle'}" font-size="11" fill="var(--text-muted)">${H[i].d}</text>`);
  // end labels, nudged apart
  const ends = series.map(s => ({s, v: s.vals[s.vals.length-1]})).filter(e => e.v != null).map(e => ({...e, yy: y(e.v)}));
  if (ends.length === 2 && Math.abs(ends[0].yy - ends[1].yy) < 14) { const mid = (ends[0].yy+ends[1].yy)/2, up = ends[0].yy < ends[1].yy ? 0 : 1;
    ends[up].yy = mid-7; ends[1-up].yy = mid+7; }
  series.forEach(s => {
    const pts = s.vals.map((v,i) => v==null ? null : `${x(i)},${y(v)}`).filter(Boolean).join(' ');
    g += `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  });
  ends.forEach(e => g += `<text x="${W-m.r+8}" y="${e.yy+4}" font-size="12" fill="var(--text-primary)">${e.s.name.replace('Paper account','Paper')} ${(e.v-100>=0?'+':'')}${(e.v-100).toFixed(1)}%</text>`);
  g += `<line id="xh" y1="${m.t}" y2="${Ht-m.b}" stroke="var(--text-muted)" visibility="hidden"/>`;
  series.forEach((s,k) => g += `<circle id="dot${k}" r="4" fill="${s.color}" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>`);
  g += `<rect x="${m.l}" y="${m.t}" width="${W-m.l-m.r}" height="${Ht-m.t-m.b}" fill="transparent" id="hit"/>`;
  box.insertAdjacentHTML('afterbegin', `<svg viewBox="0 0 ${W} ${Ht}" role="img" aria-label="Paper account vs S&P 500, indexed to 100">${g}</svg>`);
  const tip = $('#tip'), svg = box.querySelector('svg');
  const move = ev => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
    const i = Math.max(0, Math.min(H.length-1, Math.round((px-m.l)/((W-m.l-m.r)/Math.max(1,H.length-1)))));
    svg.querySelector('#xh').setAttribute('x1', x(i)); svg.querySelector('#xh').setAttribute('x2', x(i));
    svg.querySelector('#xh').setAttribute('visibility','visible');
    series.forEach((s,k) => { const d = svg.querySelector('#dot'+k), v = s.vals[i];
      if (v==null) { d.setAttribute('visibility','hidden'); return; }
      d.setAttribute('cx', x(i)); d.setAttribute('cy', y(v)); d.setAttribute('visibility','visible'); });
    tip.innerHTML = `<b>${H[i].d}</b><br>` + series.map(s => `<span style="color:${s.color}">━</span> ${s.name}: ${s.vals[i]==null?'n/a':(s.vals[i]-100>=0?'+':'')+(s.vals[i]-100).toFixed(1)+'%'}`).join('<br>') + `<br>Account ${usd(H[i].eq,0)}`;
    tip.style.display = 'block';
    const left = (x(i) / W) * r.width; tip.style.left = Math.min(left + 12, r.width - tip.offsetWidth - 4) + 'px'; tip.style.top = '8px';
  };
  const hide = () => { tip.style.display='none'; svg.querySelector('#xh').setAttribute('visibility','hidden'); series.forEach((s,k)=>svg.querySelector('#dot'+k).setAttribute('visibility','hidden')); };
  svg.querySelector('#hit').addEventListener('pointermove', move);
  svg.querySelector('#hit').addEventListener('pointerleave', hide);
  $('#chartTable').innerHTML = `<table><tr><th>Date</th><th class="n">Account</th><th class="n">Paper</th><th class="n">S&amp;P 500</th></tr>` +
    H.map((r,i) => `<tr><td>${r.d}</td><td class="n">${usd(r.eq,0)}</td><td class="n">${pct(series[0].vals[i]/100-1)}</td><td class="n">${series[1].vals[i]==null?'n/a':pct(series[1].vals[i]/100-1)}</td></tr>`).join('') + '</table>';
}
drawChart(); addEventListener('resize', () => { clearTimeout(window._rz); window._rz = setTimeout(drawChart, 150); });
if (['signals','paper','journal','screener'].includes(location.hash.slice(1))) showTab(location.hash.slice(1));
$('#tblToggle').onclick = () => { const t = $('#chartTable'); t.hidden = !t.hidden; $('#tblToggle').textContent = t.hidden ? 'Show as table' : 'Hide table'; };

const P = D.positions || [];
$('#positions').innerHTML = P.length ? `<table><tr><th>Ticker</th><th class="n">Shares</th><th class="n">Entry</th><th class="n">Price</th><th class="n">P&amp;L</th></tr>` +
  P.map(p => `<tr><td>${esc(p.ticker)}</td><td class="n">${p.qty}</td><td class="n">${usd(p.entry)}</td><td class="n">${usd(p.price)}</td><td class="n">${pct(p.pnl_pct)}</td></tr>`).join('') + '</table>'
  : '<div class="empty">No open paper positions.</div>';

// ---------- journal ----------
const J = D.journal || [];
$('#journalTbl').innerHTML = J.length ? `<table><tr><th>Date</th><th>Action</th><th>Ticker</th><th class="n">Qty</th><th class="n">~Price</th><th>Signal</th><th>Reason</th></tr>` +
  J.map(r => `<tr><td>${esc(r.date)}</td><td><b>${esc(r.action)}</b></td><td>${esc(r.ticker)}</td><td class="n">${esc(r.qty)}</td><td class="n">${usd(+r.approx_price)}</td><td>${esc(r.signal)}</td><td>${esc(r.reason)}</td></tr>`).join('') + '</table>'
  : '<div class="empty">No paper trades yet.</div>';

// ---------- screener ----------
const SC = D.screen || [];
$('#screenDate').textContent = (D.screen_date ? `(${D.screen_date})` : '') +
  (D.scan ? ` · scanned the ${D.scan.scanned} largest US companies: ${D.scan.passed} passed, ${D.scan.skipped_financials} banks/insurers/REITs skipped. Table shows passes + your watchlist.` : '');
$('#screenTbl').innerHTML = SC.length ? `<table><tr><th>Ticker</th><th>Passes</th><th class="n">Price</th><th class="n">Base value</th><th class="n">Margin of safety</th><th class="n">Rev growth</th><th>Why not</th></tr>` +
  SC.map(r => `<tr><td><b>${esc(r.ticker)}</b>${r.source==='scan' ? ' <span class="sub">· scan</span>' : ''}<br><span class="sub">${esc(r.name)}</span></td><td>${r.passes==='True'?'✅ yes':'no'}</td><td class="n">${usd(+r.price)}</td><td class="n">${!r.value_base?'n/a':(+r.value_base<=0?'negative':usd(+r.value_base))}</td><td class="n">${r.value_base && +r.value_base<=0?'n/a':pct(r.margin_of_safety)}</td><td class="n">${pct(r.revenue_growth)}</td><td class="sub">${esc(r.why_not)}</td></tr>`).join('') + '</table>'
  : '<div class="empty">No screen yet. It runs every Saturday.</div>';
const T = D.theses || {};
$('#theses').innerHTML = Object.keys(T).length ? Object.keys(T).sort().map(k => `<details><summary>${esc(k)}</summary><div class="body">${T[k]}</div></details>`).join('')
  : '<div class="empty">No thesis drafts yet.</div>';
</script>
</body>
</html>"""


def main():
    data = json.dumps(gather(), default=str).replace("</", "<\\/")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(PAGE.replace("__DATA__", data), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    print(f"Dashboard written to {DOCS / 'index.html'}")


if __name__ == "__main__":
    main()
