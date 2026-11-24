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
    card = read_json(OUT / "scorecard.json", None)
    shadow = read_json(OUT / "shadow.json", None)
    scalp = read_json(OUT / "scalp_summary.json", None)
    scalp_hist = read_csv(OUT / "scalp_history.csv")
    scalp_trades = read_csv(OUT / "scalp_trades.csv")
    wl = HERE / "watchlist.txt"
    watch = [l.strip().upper() for l in wl.read_text().splitlines() if l.strip() and not l.startswith("#")] if wl.exists() else []
    # screener table: passes + your watchlist (a 500-row table would be unreadable)
    screen = [r for r in screen if r.get("passes") == "True" or r.get("ticker") in watch or r.get("source") != "scan"]
    return {"signals": signals, "history": history, "positions": positions,
            "journal": list(reversed(journal))[:200], "screen": screen, "screen_date": screen_date,
            "theses": theses, "repo": repo, "scan": scan, "card": card, "shadow": shadow,
            "scalp": scalp, "scalp_hist": [h for h in scalp_hist if h.get("phase") == "trial"],
            "scalp_trades": list(reversed(scalp_trades))[:300]}


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
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a; --grid:#e8e7e1;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b; --neutral:#9a998f; --info:#2a78d6;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#121211; --surface-1:#1a1a19; --surface-2:#232321; --border:#33332f;
    --text-primary:#f0efec; --text-secondary:#c3c2b7; --text-muted:#8e8d84;
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --grid:#2c2c29; --neutral:#77766e; --info:#3987e5;
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
.idea{font-size:13px;margin-top:8px;padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:var(--surface-2)}
.idea.bullish{border-left:3px solid var(--good);border-radius:0 8px 8px 0}
.idea.bearish{border-left:3px solid var(--critical);border-radius:0 8px 8px 0}
.idea ul{padding-left:18px !important;list-style:disc !important;color:var(--text-primary) !important}
.qual{font-size:13px;color:var(--text-secondary);margin-top:8px;padding-top:8px;border-top:1px solid var(--border)}
.qual b{color:var(--text-primary)}
.card ul.flags{margin-top:6px;color:var(--text-secondary);font-size:12px}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.panel h2{font-size:16px;margin:0 0 8px}
.al td.tier{padding-top:12px;font-weight:650;border-bottom:0}
.al td.tier .sub{font-weight:400}
.al .num{color:var(--text-muted);width:28px}
.al a{font-weight:700;text-decoration:none}
.tierlbl{font-size:12px;font-weight:650;color:var(--text-secondary);margin-bottom:2px}
.card:target{outline:2px solid var(--info)}
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
  <button role="tab" data-tab="shadow" aria-selected="false">Shadow AI</button>
  <button role="tab" data-tab="scalping" aria-selected="false">Scalping</button>
</nav>

<section id="signals" class="on">
  <div class="panel" id="actionList" hidden></div>
  <div class="tiles" id="sigTiles"></div>
  <p class="sub" style="margin:-6px 0 12px">Green needs good fundamentals + 3 of 5 daily chart checks. Each ★ adds conviction: ★ the weekly chart agrees, ★ insiders or an activist are buying (SEC filings). Quality /6 checks return on capital, cash conversion, EPS vs revenue, interest cover, current ratio and retained earnings.</p>
  <div class="chips" id="sigChips"></div>
  <div class="cards" id="sigCards"></div>
</section>

<section id="paper">
  <div class="tiles" id="paperTiles"></div>
  <div class="panel" id="scorecard" hidden><h2>Go-live scorecard <span class="sub" id="cardSub"></span></h2><div id="cardBody"></div></div>
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

<section id="shadow">
  <p class="sub" style="margin:0 0 12px"><b>Shadow mode:</b> two self-learning models watch alongside the system but never change a signal or place a trade. Every flag is logged and checked against what actually happened, so after ~3 months you can see whether they would have helped.</p>
  <div class="panel" id="moodPanel"><h2>Market mood <span class="sub" id="moodSub"></span></h2><div id="moodBody"></div></div>
  <div class="panel"><h2>Odd accounting <span class="sub" id="oddSub"></span></h2><div id="oddBody"></div></div>
  <div class="panel"><h2>Track record <span class="sub">(do flagged companies lag the S&amp;P 500 afterwards?)</span></h2><div id="trackBody"></div></div>
</section>

<section id="scalping">
  <p class="sub" style="margin:0 0 12px"><b>Scalping replay:</b> the three setups from your scalping framework, replayed every day on SPY 1-minute bars (09:30&ndash;11:00 New York, 14:30&ndash;16:00 UK) with a &pound;5,000 demo account, 0.5% risk per trade and all the loss limits. It tests the rules perfectly; your own demo journal tests you following them. <span id="scalpUpd"></span></p>
  <div class="tiles" id="scalpTiles"></div>
  <div class="panel" id="scalpCard" hidden><h2>30-day scorecard <span class="sub" id="scalpCardSub"></span></h2><div id="scalpCardBody"></div></div>
  <div class="panel">
    <h2>Scalping vs Value Signals vs S&amp;P 500 <span class="sub">(indexed to 100 at the scalping trial start)</span></h2>
    <div class="legend" id="cmpLegend"></div>
    <div class="chart" id="cmpChart"><div class="tip" id="cmpTip"></div></div>
    <button class="linkish" id="cmpToggle">Show as table</button>
    <div class="tablewrap" id="cmpTable" hidden></div>
  </div>
  <div class="panel"><h2>Results by setup</h2><div class="tablewrap" id="setupTbl"></div><p class="sub" id="btNote" style="margin-top:8px"></p></div>
  <div class="panel"><h2>Every replayed trade, newest first</h2><div class="tablewrap" id="scalpTrades"></div></div>
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
  if (id === 'scalping') setTimeout(drawCmp, 0);
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
const tierOf = s => s.rank ? s.rank.tier : 8;
const stocks = [...S.stocks].sort((a,b) => tierOf(a)-tierOf(b) || ((b.rank||{}).strength||0)-((a.rank||{}).strength||0)
  || ORDER[a.signal]-ORDER[b.signal] || (b.stars||0)-(a.stars||0) || b.score-a.score);
(function actionList(){
  const act = stocks.filter(s => tierOf(s) <= 6);
  const el = $('#actionList');
  if (!stocks.some(s => s.rank)) return;
  el.hidden = false;
  if (!act.length) { el.innerHTML = '<h2>Action list</h2><div class="empty">Nothing to act on. Every stock is hold or ignore.</div>'; return; }
  let last = null, rows = '';
  act.forEach((s, i) => {
    const r = s.rank;
    if (r.tier !== last) { last = r.tier;
      rows += `<tr><td colspan="3" class="tier">${r.icon} ${esc(r.name)} <span class="sub">${esc(r.action)}</span></td></tr>`; }
    rows += `<tr><td class="num">${i+1}</td><td><a href="#card-${esc(s.ticker)}">${esc(s.ticker)}</a>${s.holding ? ' <span class="sub">held</span>' : ''}</td><td>${esc(r.reason)}</td></tr>`;
  });
  el.innerHTML = `<h2>Action list <span class="sub">strongest and most urgent first. Work down from #1.</span></h2>
    <div class="tablewrap"><table class="al">${rows}</table></div>`;
})();
const isSmart = s => s.smart && s.smart.confirms && s.signal !== 'GREY';
const hasIdea = s => s.option_idea && s.option_idea.meets_rule;
const hasTrig = s => s.trig && s.trig.fired;
const counts = {}; stocks.forEach(s => counts[s.signal] = (counts[s.signal]||0)+1);

$('#sigTiles').innerHTML = [['Green', counts.GREEN||0], ['Green ★★', stocks.filter(s=>s.stars===2).length],
  ['Smart money buying', stocks.filter(isSmart).length], ['Bearish ★★', stocks.filter(s => s.bear_stars===2).length], ['Warning filings', stocks.filter(hasTrig).length], ['Options ideas', stocks.filter(hasIdea).length], ['Red', counts.RED||0],
  ['Amber', counts.AMBER||0], ['Held', stocks.filter(s=>s.holding).length]]
  .map(([k,v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
let filter = 'ALL';
const chipDefs = [['ALL','All'], ['GREEN','Green'], ['SMART','Smart money'], ['IDEAS','Options ideas'], ['BEAR','Bearish setups'], ['TRIG','Warning filings'], ['AMBER','Amber'], ['RED','Red'], ['HOLD','Held'], ['GREY','Grey']];
function renderChips(){
  $('#sigChips').innerHTML = chipDefs.map(([k,l]) => `<button class="chip" data-k="${k}" aria-pressed="${filter===k}">${l}</button>`).join('');
  document.querySelectorAll('#sigChips .chip').forEach(c => c.onclick = () => { filter = c.dataset.k; renderChips(); renderCards(); });
}
function ideaBox(s){
  const o = s.option_idea; if (!o) return '';
  const d = x => '$' + (+x).toLocaleString('en-US', {maximumFractionDigits: 0});
  const head = o.meets_rule
    ? `<b>Options idea: ${esc(o.strategy)}</b> · ${o.reward_risk.toFixed(1)}× reward-to-risk`
    : `<b>No asymmetric options setup</b> at current prices (${esc(o.strategy)} only ${o.reward_risk.toFixed(1)}× reward-to-risk)`;
  const body = o.meets_rule ? `<ul style="margin:4px 0">${o.legs.map(l => `<li>${esc(l)}</li>`).join('')}</ul>
    Per contract: max loss <b>${d(o.max_loss*100)}</b> · max gain ${o.max_gain==null ? 'unlimited' : d(o.max_gain*100)} · break-even ${usd(o.breakeven)} · target ${usd(o.target)}<br>
    ${o.contracts >= 1 ? `Size for ${d(o.risk_budget)} risk: <b>${o.contracts} contract(s)</b>, max loss ${d(o.max_loss_total)}` : `1 contract risks ${d(o.max_loss*100)}, above your ${d(o.risk_budget)} limit: skip or wait`}
    <div class="sub" style="margin-top:4px">${esc(o.why)}. Prices: you pay the ask, receive the bid. Idea only, not advice.</div>` : '';
  return `<div class="idea ${o.bias}">${head}${body}</div>`;
}
function qualityLine(s){
  const q = s.quality; if (!q || !q.score_of) return '';
  const p = x => x == null ? 'n/a' : (x*100).toFixed(0) + '%';
  const bits = [`<b>Quality ${q.score}/${q.score_of}</b>`, `ROIC ${p(q.roic)}`, `cash conv ${p(q.cash_conversion)}`,
    `EPS ${q.eps_cagr==null?'n/a':(q.eps_cagr>=0?'+':'')+(q.eps_cagr*100).toFixed(0)+'%/yr'} vs revenue ${q.rev_cagr2==null?'n/a':(q.rev_cagr2>=0?'+':'')+(q.rev_cagr2*100).toFixed(0)+'%/yr'}`,
    `P/E ${q.pe==null?'n/a':q.pe.toFixed(1)}`];
  const f = s.forensic || {};
  const fb = [];
  if (f.piotroski_of) fb.push(`Piotroski ${f.piotroski}/${f.piotroski_of}`);
  if (f.altman != null) fb.push(`Altman Z ${f.altman.toFixed(1)} ${f.altman_zone === 'distress' ? '⚠ distress' : f.altman_zone}`);
  if (f.beneish != null) fb.push(`Beneish ${f.beneish.toFixed(2)}${f.beneish > -1.78 ? ' ⚠ manipulation risk' : f.beneish > -2.22 ? ' (grey)' : ' ok'}`);
  if (f.accruals != null) fb.push(`accruals ${(f.accruals*100).toFixed(0)}%`);
  return `<div class="qual">${bits.join(' · ')}${q.growth_quality ? `<br><span class="sub">${esc(q.growth_quality)}</span>` : ''}${fb.length ? `<br><span class="sub"><b>Forensic:</b> ${fb.join(' · ')}</span>` : ''}</div>`;
}
function renderCards(){
  const list = stocks.filter(s => filter==='ALL' || (filter==='SMART' ? isSmart(s) : filter==='IDEAS' ? hasIdea(s)
    : filter==='BEAR' ? (s.bear_score||0) >= 3 : filter==='TRIG' ? hasTrig(s) : s.signal===filter));
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
        (s.smart.activist ? ` <a href="${esc(s.smart.activist.url)}" target="_blank" rel="noopener">filing ↗</a>` : '') + `</li>` : '') +
      (s.trig ? `<li>${s.trig.fired ? '🚨' : '✅'} <b>warning filings:</b> ${s.trig.fired
        ? (s.trig.events||[]).slice(0,3).map(e => `${esc(e.label)} ${esc(e.date)} <a href="${esc(e.url)}" target="_blank" rel="noopener">filing ↗</a>`).join('; ')
        : esc(s.trig.text)}</li>` : '');
    return `<div class="card" id="card-${esc(s.ticker)}" style="border-left-color:${L.c}">
      ${s.rank && s.rank.tier <= 7 ? `<div class="tierlbl">${s.rank.icon} ${esc(s.rank.name)}</div>` : ''}
      <div class="top"><div><span class="tk">${esc(s.ticker)}</span> <span class="nm">${esc(s.name)}</span></div>
      <span class="badge"><span style="color:${L.c}">${L.icon}</span> ${L.label}${s.stars ? ' ' + '★'.repeat(s.stars) : ''}${s.holding ? ' · held' : ''}${hasTrig(s) ? ` · <span style="color:var(--critical)">🚨 warning filing${s.catalyst ? ' + setup' : ''}</span>` : ''}${s.source === 'scan' ? ' · found by scan' : ''}${(s.bear_score||0) >= 3 ? ` · <span style="color:var(--critical)">bearish${s.bear_stars ? ' ' + '★'.repeat(s.bear_stars) : ''}</span>` : ''}</span></div>
      <div class="val">${val} · ${s.score}/5 daily</div>
      <div class="why">${s.why.join('<br>')}</div>
      <ul>${checks}</ul>
      ${qualityLine(s)}
      ${(s.flags||[]).length ? `<ul class="flags">${s.flags.map(f => `<li>⚠ ${esc(f)}</li>`).join('')}</ul>` : ''}
      ${(s.bear_score||0) >= 3 ? `<div class="opt"><b>Bearish warning signs (${s.bear_score}):</b> ${esc((s.bear_signs||[]).join('; '))}${s.bear_wait ? '<br><b>No options idea yet:</b> the weekly chart is still rising. Wait for it to turn rather than fight the trend.' : ''}</div>` : ''}
      ${ideaBox(s)}
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
if (['signals','paper','journal','screener','shadow','scalping'].includes(location.hash.slice(1))) showTab(location.hash.slice(1));
$('#tblToggle').onclick = () => { const t = $('#chartTable'); t.hidden = !t.hidden; $('#tblToggle').textContent = t.hidden ? 'Show as table' : 'Hide table'; };

const C = D.card;
if (C) {
  $('#scorecard').hidden = false;
  $('#cardSub').textContent = `(trial started ${C.started}, decision ${C.decision_date})`;
  const mark = t => t.pass === true ? '✅' : t.pass === false ? '❌' : '➖';
  const val = t => t.value == null ? 'n/a' : t.test.startsWith('Worst drop') ? (t.value*100).toFixed(1) + '%'
    : (typeof t.value === 'number' && Math.abs(t.value) < 5 && !Number.isInteger(t.value) ? pct(t.value) : t.value);
  const tr = x => x ? `${esc(x.ticker)} ${pct(x.pnl_pct)} (${usd(x.pnl, 0)})` : 'n/a';
  $('#cardBody').innerHTML = `<p style="margin:4px 0 10px"><b>${esc(C.verdict)}</b></p>
    <table><tr><th>Test</th><th class="n">Result</th><th></th></tr>${C.tests.map(t => `<tr><td>${esc(t.test)}</td><td class="n">${val(t)}</td><td>${mark(t)}</td></tr>`).join('')}</table>
    <p class="sub" style="margin-top:10px">Win rate ${C.win_rate==null?'n/a':(C.win_rate*100).toFixed(0)+'%'} · average win ${pct(C.avg_win)} · average loss ${pct(C.avg_loss)} ·
    best ${tr(C.best)} · worst ${tr(C.worst)} · ${C.open} open, ${C.closed} closed</p>`;
}
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
$('#screenTbl').innerHTML = SC.length ? `<table><tr><th>Ticker</th><th>Passes</th><th class="n">Price</th><th class="n">Base value</th><th class="n">Margin of safety</th><th class="n">Rev growth</th><th class="n">Quality</th><th class="n">ROIC</th><th class="n">P/E</th><th class="n">F-score</th><th class="n">Altman Z</th><th class="n">Beneish M</th><th>Why not</th></tr>` +
  SC.map(r => `<tr><td><b>${esc(r.ticker)}</b>${r.source==='scan' ? ' <span class="sub">· scan</span>' : ''}<br><span class="sub">${esc(r.name)}</span></td><td>${r.passes==='True'?'✅ yes':'no'}</td><td class="n">${usd(+r.price)}</td><td class="n">${!r.value_base?'n/a':(+r.value_base<=0?'negative':usd(+r.value_base))}</td><td class="n">${r.value_base && +r.value_base<=0?'n/a':pct(r.margin_of_safety)}</td><td class="n">${pct(r.revenue_growth)}</td><td class="n">${r.quality_of && r.quality_of!=='0' ? r.quality_score+'/'+r.quality_of : 'n/a'}</td><td class="n">${r.roic ? (r.roic*100).toFixed(0)+'%' : 'n/a'}</td><td class="n">${r.pe ? (+r.pe).toFixed(1) : 'n/a'}</td><td class="n">${r.piotroski_of && r.piotroski_of!=='0' ? r.piotroski+'/'+r.piotroski_of : 'n/a'}</td><td class="n">${r.altman ? (+r.altman).toFixed(1) : 'n/a'}</td><td class="n">${r.beneish ? (+r.beneish).toFixed(2) : 'n/a'}</td><td class="sub">${esc(r.why_not)}</td></tr>`).join('') + '</table>'
  : '<div class="empty">No screen yet. It runs every Saturday.</div>';
// ---------- shadow ----------
(function shadowTab(){
  const X = D.shadow;
  if (!X) { $('#moodBody').innerHTML = $('#oddBody').innerHTML = $('#trackBody').innerHTML = '<div class="empty">Appears after the next run.</div>'; return; }
  const M = X.mood;
  const MC = {Calm:'var(--good)', Choppy:'var(--warning)', Stressed:'var(--critical)'};
  if (M) {
    $('#moodSub').textContent = `(learned from ${M.history_days} trading days since ${M.history_from})`;
    const row = k => { const s = M.stats[k]; return `<tr><td><b style="color:${MC[k]}">●</b> ${k}</td><td class="n">${(s.share*100).toFixed(0)}%</td><td class="n">${pct(s.next20_avg)}</td><td class="n">${s.next20_down_share==null?'n/a':(s.next20_down_share*100).toFixed(0)+'%'}</td><td class="n">${pct(s.next20_worst)}</td><td class="n">${(X.settings.mood_exposure[k]*100).toFixed(0)}%</td></tr>`; };
    const strip = (M.recent||[]).map(r => `<span title="${esc(r.d)}: ${esc(r.m)}" style="display:inline-block;width:1.6%;height:14px;background:${MC[r.m]};margin-right:.07%"></span>`).join('');
    $('#moodBody').innerHTML = `<div class="tiles">
      <div class="tile"><div class="k">Today</div><div class="v" style="color:${MC[M.today]}">${esc(M.today)}</div></div>
      <div class="tile"><div class="k">Since</div><div class="v">${esc(M.since)}</div></div>
      <div class="tile"><div class="k">Would invest at most</div><div class="v">${(M.exposure*100).toFixed(0)}%</div></div>
      <div class="tile"><div class="k">Model confidence</div><div class="v">${(M.confidence*100).toFixed(0)}%</div></div></div>
      <div class="sub" style="margin:8px 0 4px">Last 60 trading days</div><div>${strip}</div>
      <h3 style="font-size:14px;margin:14px 0 4px">Honest track record <span class="sub">(walk-forward: ${M.oos_days||0} days since ${esc(M.oos_from||'n/a')}, each judged by a model trained only on earlier days)</span></h3>
      <div class="tablewrap"><table><tr><th>Mood</th><th class="n">Days</th><th class="n">Next 20d avg</th><th class="n">Fell</th><th class="n">Worst</th><th class="n">Max in</th></tr>${['Calm','Choppy','Stressed'].map(row).join('')}
      ${M.baseline ? `<tr><td class="sub">All days</td><td class="n">100%</td><td class="n">${pct(M.baseline.next20_avg)}</td><td class="n">${M.baseline.next20_down_share==null?'n/a':(M.baseline.next20_down_share*100).toFixed(0)+'%'}</td><td class="n">${pct(M.baseline.next20_worst)}</td><td></td></tr>` : ''}</table></div>
      <p class="sub">The mood is useful if "Stressed" rows are clearly worse than "All days" and "Calm" rows better. With under a year of out-of-sample days, treat it as early evidence; it gets more reliable every month.</p>
      <p class="sub">"Next 20d" = how the S&amp;P did in the 20 trading days after each mood. S&amp;P now: ${pct(M.now.trend_20d)} over 20 days, ${pct(M.now.vs_200d)} vs its 200-day average, ${pct(M.now.drawdown)} from its 1-year high, volatility ${(M.now.vol_20d*100).toFixed(0)}%. The live limit stays at 50% in paper.py; this only shows what the mood would choose.</p>`;
  } else $('#moodBody').innerHTML = `<div class="empty">No market data this run${X.mood_error ? ': ' + esc(X.mood_error) : ''}.</div>`;

  const O = X.odd;
  const sig = Object.fromEntries((S.stocks||[]).map(s => [s.ticker, s.signal]));
  if (O) {
    $('#oddSub').textContent = `(neural network trained on ${O.companies} companies from the ${O.screen_date} screen; most unusual ${(X.settings.odd_top_share*100).toFixed(0)}% shown)`;
    const F = [...O.flagged].sort((a,b) => (b.concern-a.concern) || b.score-a.score);
    $('#oddBody').innerHTML = F.length ? `<p class="sub" style="margin-top:0">Each line is a number that doesn't fit the rest of that company's accounts. ⚠ = unusual in the worrying direction (worth a look for puts, or a reason to avoid). No ⚠ = unusual but not obviously bad.</p>` +
      F.map(r => `<div style="border-top:1px solid var(--border);padding:10px 0">
        <div style="display:flex;justify-content:space-between;gap:8px"><div><b>${r.concern?'⚠ ':''}${esc(r.ticker)}</b> <span class="sub">${esc(r.name)}</span></div>
        <div class="sub" style="white-space:nowrap">oddness ${r.score.toFixed(2)} · ${esc(sig[r.ticker] || (r.passes ? 'passes screen' : 'not in signals'))}</div></div>
        <div style="font-size:14px;margin-top:4px">${r.reasons.map(esc).join('<br>') || '<span class="sub">unusual overall mix of numbers</span>'}</div></div>`).join('')
      : '<div class="empty">Nothing unusual this week.</div>';
  } else $('#oddBody').innerHTML = `<div class="empty">No screen yet${X.odd_error ? ': ' + esc(X.odd_error) : ''}.</div>`;

  const R = X.track_record || {};
  $('#trackBody').innerHTML = R.checked ? `<div class="tiles">
      <div class="tile"><div class="k">Flags checked</div><div class="v">${R.checked}</div></div>
      <div class="tile"><div class="k">Avg vs S&amp;P since flag</div><div class="v">${pct(R.avg_vs_spy)}</div></div>
      <div class="tile"><div class="k">Share that lagged</div><div class="v">${(R.lagged_share*100).toFixed(0)}%</div></div></div>
      <p class="sub">Useful if flagged companies clearly lag (avg well below 0%, most lagging). Worst: ${(R.worst||[]).map(w => esc(w.ticker)+' '+pct(w.vs_spy)).join(', ')}</p>`
    : `<div class="empty">${esc(R.note || 'Not enough history yet.')}</div>`;
})();

// ---------- scalping ----------
const SP = D.scalp;
const rr = x => x == null || isNaN(x) ? 'n/a' : ((+x >= 0 ? '+' : '') + (+x).toFixed(2) + 'R');
const gbp = (x, d=0) => x == null || isNaN(x) ? 'n/a' : (x < 0 ? '-£' : '£') + Math.abs(+x).toLocaleString('en-GB', {minimumFractionDigits:d, maximumFractionDigits:d});
const share = x => x == null ? 'n/a' : (x*100).toFixed(0) + '%';
let CMP = null;
if (SP) {
  $('#scalpUpd').textContent = `Updated ${SP.updated}.`;
  const T = SP.trial || {}, B = SP.backtest || {};
  $('#scalpTiles').innerHTML = [['Trial account', gbp(T.end_equity ?? 5000)], ['Trial return', pct(T.return)],
    ['Trades (trial)', T.trades || 0], ['Win rate', share(T.win_rate)], ['Average per trade', rr(T.avg_r)],
    ['Backtest, ' + (B.days || 0) + ' days', B.trades ? pct(B.return) + ' · ' + rr(B.avg_r) : 'n/a']]
    .map(([k,v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
  const C = SP.scorecard;
  if (C) {
    $('#scalpCard').hidden = false;
    $('#scalpCardSub').textContent = `(trial ${SP.trial_start} to ${SP.trial_end})`;
    const mark = t => t.pass === true ? '✅' : t.pass === false ? '❌' : '➖';
    const val = t => t.value == null ? 'n/a' : t.test.startsWith('Average') ? rr(t.value) : t.test.startsWith('Largest') ? share(t.value) : esc(t.value);
    $('#scalpCardBody').innerHTML = `<p style="margin:4px 0 10px"><b>${esc(C.verdict)}</b></p>
      <table><tr><th>Test</th><th class="n">Result</th><th></th></tr>${C.tests.map(t => `<tr><td>${esc(t.test)}</td><td class="n">${val(t)}</td><td>${mark(t)}</td></tr>`).join('')}</table>`;
  }
  const names = {A:'A · opening range breakout', B:'B · trend pullback', C1:"C1 · failed breakout of yesterday's level", C2:"C2 · breakout and retest of yesterday's level"};
  const cell = (s, k) => { const x = (s.by_setup || {})[k]; return x && x.trades ? `<td class="n">${x.trades}</td><td class="n">${share(x.win_rate)}</td><td class="n">${rr(x.avg_r)}</td><td class="n">${rr(x.total_r)}</td>` : '<td class="n">0</td><td class="n">n/a</td><td class="n">n/a</td><td class="n">n/a</td>'; };
  $('#setupTbl').innerHTML = `<table><tr><th rowspan="2">Setup</th><th colspan="4" style="text-align:center">Backtest (${esc(B.from||'')} to ${esc(B.to||'')})</th><th colspan="4" style="text-align:center">Trial</th></tr>
    <tr><th class="n">Trades</th><th class="n">Win</th><th class="n">Avg</th><th class="n">Total</th><th class="n">Trades</th><th class="n">Win</th><th class="n">Avg</th><th class="n">Total</th></tr>` +
    Object.keys(names).map(k => `<tr><td>${names[k]}</td>${cell(B,k)}${cell(T,k)}</tr>`).join('') + '</table>';
  $('#btNote').textContent = `A 1.5R target breaks even at a 40% win rate before costs. Costs are already taken off every result. Backtest worst drop ${share(B.max_drawdown)}. No news filter yet, so news days are included.`;
  const ST = D.scalp_trades || [];
  $('#scalpTrades').innerHTML = ST.length ? `<table><tr><th>Date</th><th>UK time</th><th>Setup</th><th>Side</th><th class="n">Entry</th><th class="n">Stop</th><th class="n">Target</th><th class="n">Exit</th><th>How</th><th class="n">Result</th><th class="n">P&amp;L</th><th></th></tr>` +
    ST.map(t => `<tr><td>${esc(t.date)}</td><td>${esc(t.time_uk)}</td><td>${esc(t.setup)}</td><td>${esc(t.side)}</td><td class="n">${(+t.entry).toFixed(2)}</td><td class="n">${(+t.stop).toFixed(2)}</td><td class="n">${(+t.target).toFixed(2)}</td><td class="n">${(+t.exit).toFixed(2)}</td><td>${esc(t.reason)}</td><td class="n">${rr(+t.r)}</td><td class="n">${gbp(+t.pnl, 2)}</td><td class="sub">${t.phase === 'backtest' ? 'backtest' : ''}</td></tr>`).join('') + '</table>'
    : '<div class="empty">No trades yet.</div>';

  // comparison: scalping trial vs Value Signals paper vs S&P, all indexed to 100 at the trial start
  const start = SP.trial_start;
  const sc = Object.fromEntries((D.scalp_hist || []).map(h => [h.date, +h.equity]));
  const ph = (D.history || []).map(r => ({d: r.date, eq: +r.equity, spy: r.spy ? +r.spy : null}));
  const before = ph.filter(r => r.d <= start), pBase = before.length ? before[before.length-1] : ph.find(r => r.d >= start);
  const dates = [...new Set([...Object.keys(sc), ...ph.filter(r => r.d >= start).map(r => r.d)])].sort();
  if (dates.length) {
    let s = 5000, p = pBase ? pBase.eq : null, q = pBase ? pBase.spy : null;
    const rows = dates.map(d => { if (sc[d] != null) s = sc[d]; const r = ph.find(x => x.d === d); if (r) { p = r.eq; if (r.spy) q = r.spy; }
      return {d, s, p, q}; });
    CMP = {rows: [{d: start, s: 5000, p: pBase ? pBase.eq : null, q: pBase ? pBase.spy : null}, ...rows.filter(r => r.d > start)],
      series: [{name: 'Scalping replay', color: 'var(--series-1)', key: 's', base: 5000},
               {name: 'Value Signals paper', color: 'var(--series-2)', key: 'p', base: pBase ? pBase.eq : null},
               {name: 'S&P 500', color: 'var(--series-3)', key: 'q', base: pBase ? pBase.spy : null}]};
  }
} else {
  $('#scalpTiles').innerHTML = '<div class="empty">The scalping replay hasn\'t run yet. It runs with the daily update.</div>';
}

function drawCmp(){
  const box = $('#cmpChart'); box.querySelectorAll('svg,.empty').forEach(e => e.remove());
  if (!CMP || CMP.rows.length < 2) { box.insertAdjacentHTML('afterbegin', '<div class="empty">The comparison appears after the first trial day.</div>'); return; }
  const R = CMP.rows;
  const series = CMP.series.filter(s => s.base).map(s => ({...s, vals: R.map(r => r[s.key] == null ? null : r[s.key] / s.base * 100)}));
  $('#cmpLegend').innerHTML = series.map(s => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join('');
  const W = Math.max(320, box.clientWidth), Ht = 260, m = {l:44, r:150, t:10, b:26};
  const all = series.flatMap(s => s.vals).filter(v => v != null);
  let lo = Math.min(...all, 100), hi = Math.max(...all, 100); const pad = Math.max(0.5, (hi-lo)*0.12); lo -= pad; hi += pad;
  const x = i => m.l + (W-m.l-m.r) * i / (R.length-1);
  const y = v => m.t + (Ht-m.t-m.b) * (1 - (v-lo)/(hi-lo));
  let g = '';
  for (let i=0;i<=4;i++){ const v = lo + (hi-lo)/4*i;
    g += `<line x1="${m.l}" x2="${W-m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l-8}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="var(--text-muted)">${v.toFixed(1)}</text>`; }
  g += `<line x1="${m.l}" x2="${W-m.r}" y1="${y(100)}" y2="${y(100)}" stroke="var(--text-muted)" stroke-dasharray="3 3"/>`;
  [0, Math.floor((R.length-1)/2), R.length-1].filter((v,i,a) => a.indexOf(v)===i).forEach(i =>
    g += `<text x="${x(i)}" y="${Ht-6}" text-anchor="${i===0?'start':i===R.length-1?'end':'middle'}" font-size="11" fill="var(--text-muted)">${R[i].d}</text>`);
  series.forEach(s => g += `<polyline points="${s.vals.map((v,i) => v==null ? null : x(i)+','+y(v)).filter(Boolean).join(' ')}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`);
  const ends = series.map(s => ({s, v: s.vals[s.vals.length-1]})).filter(e => e.v != null).map(e => ({...e, yy: y(e.v)})).sort((a,b) => a.yy - b.yy);
  for (let k=1;k<ends.length;k++) if (ends[k].yy - ends[k-1].yy < 14) ends[k].yy = ends[k-1].yy + 14;
  ends.forEach(e => g += `<text x="${W-m.r+8}" y="${e.yy+4}" font-size="12" fill="var(--text-primary)">${e.s.name.replace(' replay','').replace(' paper','')} ${(e.v-100>=0?'+':'')}${(e.v-100).toFixed(1)}%</text>`);
  g += `<line id="cxh" y1="${m.t}" y2="${Ht-m.b}" stroke="var(--text-muted)" visibility="hidden"/>`;
  series.forEach((s,k) => g += `<circle id="cdot${k}" r="4" fill="${s.color}" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>`);
  g += `<rect x="${m.l}" y="${m.t}" width="${W-m.l-m.r}" height="${Ht-m.t-m.b}" fill="transparent" id="chit"/>`;
  box.insertAdjacentHTML('afterbegin', `<svg viewBox="0 0 ${W} ${Ht}" role="img" aria-label="Scalping replay vs Value Signals paper vs S&P 500, indexed to 100">${g}</svg>`);
  const tip = $('#cmpTip'), svg = box.querySelector('svg');
  svg.querySelector('#chit').addEventListener('pointermove', ev => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
    const i = Math.max(0, Math.min(R.length-1, Math.round((px-m.l) / ((W-m.l-m.r)/(R.length-1)))));
    const xh = svg.querySelector('#cxh'); xh.setAttribute('x1', x(i)); xh.setAttribute('x2', x(i)); xh.setAttribute('visibility', 'visible');
    series.forEach((s,k) => { const d = svg.querySelector('#cdot'+k), v = s.vals[i];
      if (v == null) { d.setAttribute('visibility','hidden'); return; }
      d.setAttribute('cx', x(i)); d.setAttribute('cy', y(v)); d.setAttribute('visibility', 'visible'); });
    tip.innerHTML = `<b>${R[i].d}</b><br>` + series.map(s => `<span style="color:${s.color}">━</span> ${s.name}: ${s.vals[i]==null?'n/a':(s.vals[i]-100>=0?'+':'')+(s.vals[i]-100).toFixed(1)+'%'}`).join('<br>');
    tip.style.display = 'block'; tip.style.left = Math.min((x(i)/W)*r.width + 12, r.width - tip.offsetWidth - 4) + 'px'; tip.style.top = '8px';
  });
  svg.querySelector('#chit').addEventListener('pointerleave', () => { tip.style.display = 'none'; svg.querySelector('#cxh').setAttribute('visibility','hidden');
    series.forEach((s,k) => svg.querySelector('#cdot'+k).setAttribute('visibility','hidden')); });
  $('#cmpTable').innerHTML = `<table><tr><th>Date</th>${series.map(s => `<th class="n">${s.name}</th>`).join('')}</tr>` +
    R.map((r,i) => `<tr><td>${r.d}</td>${series.map(s => `<td class="n">${s.vals[i]==null?'n/a':pct(s.vals[i]/100-1)}</td>`).join('')}</tr>`).join('') + '</table>';
}
$('#cmpToggle').onclick = () => { const t = $('#cmpTable'); t.hidden = !t.hidden; $('#cmpToggle').textContent = t.hidden ? 'Show as table' : 'Hide table'; };
addEventListener('resize', () => { if ($('#scalping').classList.contains('on')) { clearTimeout(window._rz2); window._rz2 = setTimeout(drawCmp, 150); } });

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
