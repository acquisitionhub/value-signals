#!/usr/bin/env python3
"""
Traffic-light signals: fundamentals + technicals, sent to your phone
----------------------------------------------------------------
For each stock in watchlist.txt (buy ideas) and holdings.csv (what you own):

  GREEN  buy candidate: passes the fundamental screen AND at least 3 of 5 technical confirmations
  AMBER  watch: good company, but the chart says wait
  GREY   doesn't pass the fundamental screen, ignored whatever the chart says
  RED    sell/exit (holdings) or avoid (watchlist): target reached, thesis broken,
         price breakdown, or stop-loss hit

It never trades. It sends you the evidence and you decide.

Usage:
  python signals.py                 # weekly: summary notification + full report saved in output/
  python signals.py --mode daily    # daily: notifies ONLY if a signal changed (new green/red, strong, stop hit)
  python signals.py --no-send       # just save the report in output/

Delivery: a push notification to your phone via the free ntfy app. Set NTFY_TOPIC in
settings.txt (see README). The full report is always saved in output/. Email is optional.

Research aid only, not investment advice.
"""
import argparse, csv, json, os, smtplib, ssl, time
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd

import screener as fx  # fundamentals (same folder)

HERE = Path(__file__).parent
OUT = HERE / "output"

RULES = {
    "green_min_confirmations": 3,   # of 5
    "default_stop_loss": 0.20,      # exit if 20% below entry, unless holdings.csv sets one
    "benchmark": "SPY",             # S&P 500 ETF for relative strength
}


# --------------------------------------------------------------------------------------
# PRICE DATA
# --------------------------------------------------------------------------------------
def get_history(ticker, offline_dir=None):
    """Daily OHLCV for ~2 years. Columns: Close, Volume."""
    if offline_dir:
        p = Path(offline_dir) / f"{ticker}.csv"
        return pd.read_csv(p, index_col=0, parse_dates=True) if p.exists() else None
    import prices
    return prices.history(ticker)


def implied_vol(ticker, offline_dir=None):
    """Rough at-the-money implied volatility from the nearest expiry 30+ days out."""
    if offline_dir:
        return None
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        price = t.history(period="5d")["Close"].iloc[-1]
        exps = [e for e in t.options if (pd.Timestamp(e) - pd.Timestamp.today()).days >= 30]
        if not exps:
            return None
        calls = t.option_chain(exps[0]).calls
        row = calls.iloc[(calls["strike"] - price).abs().argsort()[:1]]
        iv = float(row["impliedVolatility"].iloc[0])
        return iv if 0.01 < iv < 5 else None
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# INDICATORS
# --------------------------------------------------------------------------------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def macd(close):
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    sig = line.ewm(span=9, adjust=False).mean()
    return line, sig


def technicals(h, bench):
    c, v = h["Close"], h["Volume"]
    sma200 = c.rolling(200).mean()
    r = rsi(c)
    ml, ms = macd(c)
    hist = ml - ms
    last = c.iloc[-1]
    checks = {}

    # 1. Trend: above 200-day average, or crossed back above in the last 10 days
    above = last > sma200.iloc[-1]
    crossed = (c.iloc[-10:] > sma200.iloc[-10:]).any() and (c.iloc[-30:-10] < sma200.iloc[-30:-10]).any()
    checks["trend"] = (above or crossed,
                       f"price {'above' if above else 'below'} 200-day average (${sma200.iloc[-1]:,.2f})"
                       + (", just crossed back above" if crossed and above else ""))

    # 2. Momentum: RSI recovering from oversold, or healthy and rising
    r_now, r_min = r.iloc[-1], r.iloc[-20:].min()
    recovering = r_min < 35 and r_now > r_min + 5
    healthy = 40 <= r_now <= 65 and r_now > r.iloc[-6]
    checks["rsi"] = (recovering or healthy,
                     f"RSI {r_now:.0f}" + (f", recovering from {r_min:.0f}" if recovering
                                           else ", rising" if healthy
                                           else " (overbought)" if r_now > 70
                                           else ", falling" if r_now <= r.iloc[-6] else ", weak"))

    # 3. MACD: crossed above signal in last 10 days, or above it with histogram rising
    cross_up = ((hist.iloc[-10:] > 0) & (hist.shift(1).iloc[-10:] <= 0)).any()
    rising = hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-4]
    checks["macd"] = (bool(cross_up or rising),
                      "MACD crossed up" if cross_up else "MACD above signal and rising" if rising
                      else "MACD below signal" if hist.iloc[-1] < 0 else "MACD momentum fading")

    # 4. Volume: up days carry more volume than down days (last 20 days)
    chg = c.diff().iloc[-20:]
    vol = v.iloc[-20:]
    up_v, dn_v = vol[chg > 0].mean(), vol[chg < 0].mean()
    ratio = (up_v / dn_v) if dn_v and dn_v == dn_v and up_v == up_v else 1.0
    checks["volume"] = (ratio > 1.2, f"up-day volume {ratio:.1f}x down-day volume")

    # 5. Relative strength: 3-month return vs S&P 500
    rs = None
    if bench is not None and len(bench) > 63:
        rs = (c.iloc[-1] / c.iloc[-64] - 1) - (bench["Close"].iloc[-1] / bench["Close"].iloc[-64] - 1)
    checks["rel_strength"] = (rs is not None and rs > 0,
                              "no benchmark data" if rs is None else
                              f"{'beating' if rs > 0 else 'lagging'} S&P 500 by {abs(rs)*100:.1f} pts over 3 months")

    # Breakdown test for RED: below 200-day, 200-day falling, and making lower lows
    low_now, low_prev = c.iloc[-20:].min(), c.iloc[-40:-20].min()
    breakdown = (not above) and sma200.iloc[-1] < sma200.iloc[-21] and low_now < low_prev
    realised_vol = c.pct_change().iloc[-30:].std() * (252 ** 0.5)

    return {"price": float(last), "checks": checks, "score": sum(ok for ok, _ in checks.values()),
            "weekly": weekly_view(h),
            "breakdown": bool(breakdown), "realised_vol": float(realised_vol)}


def weekly_view(h):
    """Bigger picture from the weekly chart: 30-week average trend + weekly MACD."""
    w = h["Close"].resample("W-FRI").last().dropna()
    if len(w) < 40:
        return {"confirms": False, "text": "not enough weekly history"}
    sma30 = w.rolling(30).mean()
    ml, ms = macd(w)
    above = w.iloc[-1] > sma30.iloc[-1]
    rising = sma30.iloc[-1] > sma30.iloc[-5]
    mom = (ml - ms).iloc[-1] > 0
    ok = bool(above and rising and mom)
    parts = [f"{'above' if above else 'below'} 30-week average",
             f"30-week average {'rising' if rising else 'falling'}",
             f"weekly MACD {'positive' if mom else 'negative'}"]
    return {"confirms": ok, "text": ", ".join(parts)}


# --------------------------------------------------------------------------------------
# SIGNAL RULES
# --------------------------------------------------------------------------------------
def thesis_broken(m):
    reasons = []
    if m["fcf_latest"] is not None and m["fcf_latest"] < 0:
        reasons.append("free cash flow negative in the latest year")
    if m["revenue_growth"] is not None and m["revenue_growth"] < 0:
        reasons.append("revenue fell in the latest year")
    h = m["history"]
    if len(h) >= 3 and h[-3]["debt"] and h[-1]["debt"] > 1.2 * h[-3]["debt"]:
        reasons.append("debt up more than 20% in two years")
    return reasons


def options_note(t, iv):
    if iv is None:
        return "Implied volatility unavailable."
    rv = t["realised_vol"]
    if iv < rv * 1.1:
        view = "options look cheap; buying long-dated calls fits"
    elif iv > rv * 1.3:
        view = "options look expensive; selling puts or spreads fits"
    else:
        view = "options fairly priced"
    return f"Implied vol {iv*100:.0f}% vs recent actual {rv*100:.0f}%: {view}."


def decide(m, t, holding=None):
    price = t["price"]
    if holding:   # SELL side: any one red reason is enough
        red = []
        if m and m.get("iv_base") is not None and m["iv_base"] <= 0:
            red.append("debt now exceeds the estimated value of the business")
        elif m and m.get("iv_base") and price >= m["iv_base"]:
            red.append(f"price ${price:,.2f} has reached base value ${m['iv_base']:,.2f} (target hit)")
        if m:
            red += thesis_broken(m)
        if t["breakdown"]:
            red.append("price breakdown: below a falling 200-day average and making lower lows")
        stop = holding.get("stop") or holding["entry"] * (1 - RULES["default_stop_loss"])
        if price <= stop:
            red.append(f"stop-loss hit (stop ${stop:,.2f}, entry ${holding['entry']:,.2f})")
        if red:
            return "RED", red
        pnl = price / holding["entry"] - 1
        why = [f"no exit signal; {pnl*100:+.1f}% vs your entry ${holding['entry']:,.2f}"]
        if not t["weekly"]["confirms"]:
            why.append("watch: weekly trend no longer fully supportive")
        return "HOLD", why

    # BUY side
    if m is None or not m["passes"]:
        return "GREY", ["fails fundamentals: " + ("; ".join(m["fail_reasons"]) if m else "no SEC data")]
    if t["breakdown"]:
        return "RED", ["good fundamentals but the price is breaking down (below a falling 200-day "
                       "average, lower lows): avoid for now"]
    if t["score"] >= RULES["green_min_confirmations"]:
        if t["weekly"]["confirms"]:
            return "GREEN", [f"fundamentals pass, {t['score']}/5 daily confirmations "
                             "<b>and the weekly chart agrees (strong)</b>"]
        return "GREEN", [f"fundamentals pass, {t['score']}/5 daily confirmations; "
                         "weekly chart not yet confirming"]
    return "AMBER", [f"fundamentals pass, only {t['score']}/5 technical confirmations: wait"]


# --------------------------------------------------------------------------------------
# EMAIL
# --------------------------------------------------------------------------------------
COLOURS = {"GREEN": "#1a7f37", "AMBER": "#b26a00", "RED": "#c62828", "GREY": "#6b7280", "HOLD": "#2563eb"}
ICON = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴", "GREY": "⚪", "HOLD": "🔵"}
ORDER = {"RED": 0, "GREEN": 1, "AMBER": 2, "HOLD": 3, "GREY": 4}


def card(r):
    col = COLOURS[r["signal"]]
    m, t = r["m"], r["t"]
    val = ""
    if m and m.get("iv_base") is not None and m["iv_base"] <= 0:
        val = f"Value: <b>negative</b> (debt exceeds estimated business value) vs price <b>${t['price']:,.2f}</b>"
    elif m and m.get("iv_base"):
        val = (f"Value: bear ${m['iv_bear']:,.2f} / <b>base ${m['iv_base']:,.2f}</b> / bull ${m['iv_bull']:,.2f}"
               f" vs price <b>${t['price']:,.2f}</b>")
    checks = "".join(f"<li>{'✅' if ok else '❌'} {txt}</li>" for ok, txt in t["checks"].values())
    wk = t["weekly"]
    checks += f"<li>{'✅' if wk['confirms'] else '❌'} <b>weekly chart:</b> {wk['text']}</li>"
    sm = r.get("smart")
    if sm:
        checks += f"<li>{'✅' if sm['confirms'] else '➖'} <b>smart money:</b> {sm['text']}</li>"
    badge = f" {stars(r)}" if r.get("stars") else ""
    change = f"<div style='color:#555;font-size:13px'>Was: {r['prev']}</div>" if r.get("prev") else ""
    why = "".join(f"<li>{x}</li>" for x in r["why"])
    extra = f"<p style='margin:6px 0;color:#444'>{r['opt']}</p>" if r["signal"] in ("GREEN", "AMBER") else ""
    return f"""
<div style="border-left:6px solid {col};padding:10px 14px;margin:12px 0;background:#fafafa">
  <div style="font-size:17px"><b>{ICON[r['signal']]} {r['ticker']}</b> &middot; {r['name']}
    <span style="color:{col};font-weight:bold">&nbsp;{r['signal']}{badge}</span>
    <span style="color:#666">&nbsp;({t['score']}/5 daily technicals)</span></div>
  {change}
  <div style="margin:4px 0">{val}</div>
  <ul style="margin:4px 0 4px 18px;padding:0">{why}</ul>
  <ul style="margin:4px 0 4px 18px;padding:0;color:#333;font-size:13px;list-style:none">{checks}</ul>
  {extra}
</div>"""


def stars(r):
    return "★" * r.get("stars", 0)


def label(r):
    st = r.get("stars", 0)
    return r["signal"] + (f" {stars(r)}" if st else "")


def build_email(results, mode="weekly", changes=None):
    changes = changes or []
    foot = """<p style="color:#888;font-size:12px;margin-top:28px">Signals line up evidence. They are not predictions or advice.
Check the filing and the chart yourself before you trade. Fundamentals from SEC 10-K data; prices from Yahoo Finance.</p>"""
    head = lambda title, sub: (f'<html><body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;color:#111">'
                               f'<h1 style="margin-bottom:4px">{title} &middot; {date.today().strftime("%d %b %Y")}</h1>'
                               f'<p style="color:#555;margin-top:0">{sub}</p>')
    sec = lambda title, rs: (f"<h2 style='margin-top:24px'>{title}</h2>" + "".join(card(r) for r in rs)) if rs else ""

    if mode == "daily":
        html = head("Daily signal changes", f"{len(changes)} change(s) since the last check") + \
               "".join(card(r) for r in sorted(changes, key=lambda r: ORDER[r["signal"]])) + foot + "</body></html>"
        first = changes[0] if changes else None
        subject = (f"Signal change: {first['ticker']} {label(first)}" if len(changes) == 1
                   else f"Signal changes: {len(changes)} stocks") if changes else "No signal changes"
        return subject, html

    hold = sorted([r for r in results if r["holding"]], key=lambda r: ORDER[r["signal"]])
    watch = sorted([r for r in results if not r["holding"]],
                   key=lambda r: (ORDER[r["signal"]], not r.get("strong"), -r["t"]["score"]))
    counts = {k: sum(r["signal"] == k for r in results) for k in ICON}
    strong = sum(1 for r in results if r.get("strong"))
    summary = " &nbsp; ".join(f"{ICON[k]} {k.title()}: {v}" for k, v in counts.items() if v)
    if strong:
        summary += f" &nbsp; &#9733; Strong: {strong}"
    chg = ""
    if changes:
        chg = "<h2 style='margin-top:24px'>Changed since last check</h2><ul>" + "".join(
            f"<li><b>{r['ticker']}</b>: {r['prev']} &rarr; <b>{label(r)}</b></li>" for r in changes) + "</ul>"
    html = head("Weekly signals", summary) + chg + sec("Your holdings", hold) + sec("Watchlist", watch) + foot + "</body></html>"
    subject = f"Weekly signals {date.today().strftime('%d %b')}: {counts.get('GREEN', 0)} green ({strong} strong), {counts.get('RED', 0)} red"
    return subject, html


def load_settings():
    """Read settings.txt (KEY=value lines) into the environment if not already set."""
    p = HERE / "settings.txt"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def send_email(subject, html):
    """Optional: only used if SMTP settings are filled in."""
    load_settings()
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        return False
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText("Open this email in HTML view to see the signals.", "plain"))
    msg.attach(MIMEText(html, "html"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
            s.login(user, pw); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            if os.environ.get("SMTP_STARTTLS", "1") == "1":
                s.starttls(context=ssl.create_default_context())
            if pw != "-":
                s.login(user, pw)
            s.send_message(msg)
    print(f"Email sent to {to}")
    return True


# --------------------------------------------------------------------------------------
# PHONE NOTIFICATIONS (ntfy)
# --------------------------------------------------------------------------------------
def short_line(r):
    t, m = r["t"], r["m"]
    val = ""
    if m and m.get("iv_base") is not None and m["iv_base"] > 0:
        val = f" vs value ${m['iv_base']:,.2f}"
    prev = f"{r['prev']} -> " if r.get("prev") else ""
    wk = "weekly ok" if t["weekly"]["confirms"] else "weekly no"
    icon = ICON[r["signal"]]
    line = f"{icon} {r['ticker']}: {prev}{label(r)} | ${t['price']:,.2f}{val} | {t['score']}/5 daily, {wk}"
    sm = r.get("smart")
    if sm and sm["confirms"]:
        bits = []
        if sm["cluster"]:
            bits.append(f"{sm['buyers']} insiders bought ${sm['total']:,.0f}")
        if sm["activist"]:
            bits.append("activist 13D " + sm["activist"]["date"] + (f" ({sm['activist']['who']})" if sm["activist"]["who"] else ""))
        line += "\n   Smart money: " + "; ".join(bits)
    oi = r.get("option_idea")
    if oi and oi.get("meets_rule"):
        if oi["bias"] == "bearish":
            line += f"\n   Bearish setup: {'; '.join(r.get('bear_signs', [])[:3])}"
        line += (f"\n   Options idea: {oi['strategy']} {oi['expiry']}, max loss ${oi['max_loss']*100:,.0f}/contract, "
                 f"{oi['reward_risk']:.1f}x reward-to-risk")
    if r["signal"] == "RED":
        line += "\n   " + r["why"][0].replace("<b>", "").replace("</b>", "")
    return line


def build_notification(results, mode, changes, extra=None):
    items_txt = ""
    if mode == "daily":
        items = sorted(changes, key=lambda r: ORDER[r["signal"]])
        if len(items) == 1:
            r0 = items[0]
            if "new smart-money" in (r0.get("prev") or ""):
                title = f"Smart money buying {r0['ticker']} ({r0['signal'].lower()}, {r0.get('stars', 0)} stars)"
            else:
                title = f"Signal change: {r0['ticker']} now {r0['signal']}" + (f" ({r0['stars']} stars)" if r0.get("stars") else "")
        else:
            title = f"Signal changes: {len(items)} stocks"
    else:
        items = sorted([r for r in results if r["signal"] in ("RED", "GREEN")
                        or (r["signal"] in ("AMBER", "HOLD") and r.get("smart") and r["smart"]["confirms"])
                        or (r.get("option_idea") and r["option_idea"].get("meets_rule"))],
                       key=lambda r: (ORDER[r["signal"]], -r.get("stars", 0)))
        g = sum(r["signal"] == "GREEN" for r in results)
        st = sum(1 for r in results if r.get("strong"))
        rd = sum(r["signal"] == "RED" for r in results)
        am = sum(r["signal"] == "AMBER" for r in results)
        sm_n = sum(1 for r in results if r.get("smart") and r["smart"]["confirms"] and r["signal"] != "GREY")
        two = sum(1 for r in results if r.get("stars") == 2)
        ideas_n = sum(1 for r in results if r.get("option_idea") and r["option_idea"].get("meets_rule"))
        title = (f"Weekly signals: {g} green ({two} two-star), {rd} red" + (f", smart money on {sm_n}" if sm_n else "")
                 + (f", {ideas_n} options ideas" if ideas_n else ""))
        if not items:
            items_txt = f"Nothing green or red this week. {am} amber on the watchlist."
    if mode == "daily" and not items:
        items_txt = "No signal changes."
        title = "Paper trades placed"
    body = "\n".join(short_line(r) for r in items) if items else items_txt
    if extra:
        body += "\n\n" + "\n".join(extra)
    body += "\n\nFull report saved on your computer (output folder)."
    urgent = any(r["signal"] == "RED" and r["holding"] for r in items)
    tag = ("rotating_light" if urgent else "red_circle" if any(r["signal"] == "RED" for r in items)
           else "green_circle" if any(r["signal"] == "GREEN" for r in items) else "bar_chart")
    return title, body, ("high" if urgent else "default"), tag


def send_notification(title, body, priority="default", tag="bar_chart"):
    load_settings()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and os.environ.get("GITHUB_ACTIONS"):
        print("Notification not sent: add NTFY_TOPIC as a GitHub secret (see README).")
        return False
    if not topic:
        import secrets
        topic = "signals-" + secrets.token_hex(8)
        p = HERE / "settings.txt"
        if not p.exists() and (HERE / "settings.example.txt").exists():
            p.write_text((HERE / "settings.example.txt").read_text())
        txt = p.read_text() if p.exists() else "NTFY_TOPIC=\n"
        txt = txt.replace("NTFY_TOPIC=\n", f"NTFY_TOPIC={topic}\n", 1) if "NTFY_TOPIC=\n" in txt \
            else f"NTFY_TOPIC={topic}\n" + txt
        p.write_text(txt)
        os.environ["NTFY_TOPIC"] = topic
        print(f"\n*** New private topic created: {topic}\n*** In the ntfy app tap +, subscribe to that exact name.\n")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    import requests
    # headers must be plain ASCII; emoji go in the body and the Tags header
    safe_title = title.encode("ascii", "ignore").decode()
    r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"), timeout=20,
                      headers={"Title": safe_title, "Priority": priority, "Tags": tag})
    r.raise_for_status()
    print(f"Notification sent to ntfy topic '{topic}'")
    return True


# --------------------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------------------
def read_holdings():
    p = HERE / "holdings.csv"
    out = {}
    if p.exists():
        with p.open() as f:
            for row in csv.DictReader(l for l in f if not l.startswith("#")):
                try:
                    out[row["ticker"].strip().upper()] = {
                        "entry": float(row["entry_price"]),
                        "stop": float(row["stop_loss"]) if row.get("stop_loss") else None}
                except (KeyError, TypeError, ValueError, AttributeError):
                    pass
    return out


class CachedEdgar(fx.Edgar):
    """Fundamentals only change quarterly, so reuse downloaded SEC data for 7 days."""
    def facts(self, cik):
        if self.offline_dir:
            return super().facts(cik)
        d = HERE / "cache"; d.mkdir(exist_ok=True)
        p = d / f"CIK{cik:010d}.json"
        if p.exists() and time.time() - p.stat().st_mtime < 7 * 86400:
            return json.loads(p.read_text())
        data = super().facts(cik)
        p.write_text(json.dumps(data))
        return data


STATE_FILE = "state.json"


def load_state():
    p = OUT / STATE_FILE
    return json.loads(p.read_text()) if p.exists() else None


def save_state(results):
    OUT.mkdir(exist_ok=True)
    (OUT / STATE_FILE).write_text(json.dumps(
        {r["ticker"]: {"signal": r["signal"], "strong": bool(r.get("strong")), "stars": r.get("stars", 0),
                       "smart": bool(r.get("smart") and r["smart"]["confirms"]),
                       "idea": bool(r.get("option_idea") and r["option_idea"]["meets_rule"]),
                       "date": date.today().isoformat()}
         for r in results}, indent=1))


def save_latest(results, mode, paper_mode):
    """Machine-readable snapshot for the dashboard."""
    from datetime import datetime, timezone
    rows = []
    for r in results:
        m, t = r["m"], r["t"]
        rows.append({
            "ticker": r["ticker"], "name": r["name"], "signal": r["signal"], "strong": bool(r.get("strong")),
            "stars": r.get("stars", 0), "smart": r.get("smart"),
            "holding": r["holding"], "source": r.get("source"), "why": r["why"], "price": t["price"], "score": t["score"],
            "checks": [{"ok": bool(ok), "text": txt} for ok, txt in t["checks"].values()],
            "weekly": t["weekly"], "options": r.get("opt") or "",
            "value": None if not m else {"bear": m.get("iv_bear"), "base": m.get("iv_base"), "bull": m.get("iv_bull")},
            "quality": None if not m else {k: (m.get("quality") or {}).get(k) for k in
                       ("score", "score_of", "roic", "cash_conversion", "eps_cagr", "rev_cagr2", "pe",
                        "interest_cover", "current_ratio", "growth_quality", "checks")},
            "flags": [] if not m else [f for f in m.get("red_flags", []) if not f.startswith("Data:")][:4],
            "bear_score": r.get("bear_score", 0), "bear_signs": r.get("bear_signs", []),
            "option_idea": r.get("option_idea"),
        })
    OUT.mkdir(exist_ok=True)
    (OUT / "latest_signals.json").write_text(json.dumps({
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "mode": mode,
        "paper": paper_mode, "stocks": rows}, indent=1, default=str))


def find_changes(results, state):
    out = []
    for r in results:
        old = state.get(r["ticker"])
        if old is None:
            if r["signal"] in ("GREEN", "RED"):   # newly added stock already flashing
                r["prev"] = "new to list"; out.append(r)
            continue
        smart_now = bool(r.get("smart") and r["smart"]["confirms"])
        prev_label = old["signal"] + (" " + "★" * old.get("stars", 0) if old.get("stars") else "")
        if not old.get("smart") and smart_now and r["signal"] != "GREY":
            r["prev"] = prev_label + " (new smart-money buying)"; out.append(r); continue
        idea_now = bool(r.get("option_idea") and r["option_idea"]["meets_rule"])
        if not old.get("idea") and idea_now:
            r["prev"] = prev_label + " (new options idea)"; out.append(r); continue
        if old["signal"] == "GREEN" and r["signal"] == "HOLD":
            continue   # just means you (or the paper account) bought it
        if old["signal"] != r["signal"] or old.get("stars", 0) != r.get("stars", 0):
            r["prev"] = prev_label
            # ignore moves that don't need action (e.g. grey <-> amber)
            if {"GREEN", "RED"} & {old["signal"], r["signal"]}:
                out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["weekly", "daily"], default="weekly",
                    help="weekly = full report; daily = email only when a signal changes")
    ap.add_argument("--no-send", "--no-email", dest="no_send", action="store_true",
                    help="don't notify; just save the report in output/")
    ap.add_argument("--paper", action="store_true",
                    help="trade the signals on your Alpaca PAPER account (uses paper positions as holdings)")
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--offline-dir", help=argparse.SUPPRESS)   # testing: saved EDGAR JSON + price CSVs
    a = ap.parse_args()

    global STATE_FILE
    load_settings()
    api = None
    if a.paper:
        import paper
        api = paper.Alpaca()
        holdings = paper.paper_holdings(api)
        STATE_FILE = "state_paper.json"
        print(f"Paper account: {len(holdings)} open positions")
    else:
        holdings = read_holdings()
    discovered = []
    if a.tickers:
        watch = [t.upper() for t in a.tickers]
    else:
        watch = fx.read_watchlist()
        for fn in ("discovered.txt", "bearish.txt"):   # written by the weekly scan (screener.py --top)
            dp = OUT / fn
            if dp.exists():
                discovered += [l.split("#")[0].strip().upper() for l in dp.read_text().splitlines()
                               if l.strip() and not l.startswith("#")]
    tickers = list(dict.fromkeys(list(holdings) + watch + discovered))
    if discovered:
        print(f"Including {len([t for t in discovered if t not in watch])} stocks found by the weekly scan")

    ed = CachedEdgar(a.offline_dir)
    import smartmoney
    sm_engine = smartmoney.SmartMoney(ed)
    bench = get_history(RULES["benchmark"], a.offline_dir)
    results = []
    print(f"[{a.mode}] Checking {len(tickers)} stocks...")
    for tk in tickers:
        h = get_history(tk, a.offline_dir)
        if h is None:
            print(f"  {tk}: no price history"); continue
        t = technicals(h, bench)
        cik, name = ed.lookup(tk)
        m = None
        if cik:
            try:
                m = fx.analyse(tk, name, ed.facts(cik))
                if m:
                    sic, sic_desc = ed.industry(cik)
                    m["financial"], m["industry"] = fx.is_financial(sic), sic_desc
                    m["price"] = t["price"]; fx.value(m); fx.screen(m)
            except Exception as e:
                print(f"  {tk}: fundamentals error ({e})")
        sig, why = decide(m, t, holdings.get(tk))
        smart = None
        if cik:
            try:
                smart = sm_engine.check(cik)
            except Exception as e:
                print(f"  {tk}: smart-money check failed ({e})")
        import playbook
        n_bear, bear_signs = playbook.bear_score(m, t["breakdown"]) if m else (0, [])
        strong = sig == "GREEN" and t["weekly"]["confirms"]
        n_stars = (int(t["weekly"]["confirms"]) + int(bool(smart and smart["confirms"]))) if sig == "GREEN" else 0
        if sig == "GREEN" and smart and smart["confirms"]:
            why = why + ["<b>smart money is buying too</b>"]
        elif sig in ("AMBER", "HOLD") and smart and smart["confirms"]:
            why = why + ["smart money is buying: watch closely"]
        results.append({"ticker": tk, "name": name or tk, "signal": sig, "why": why, "m": m, "t": t,
                        "strong": strong, "stars": n_stars, "smart": smart, "holding": tk in holdings,
                        "bear_score": n_bear, "bear_signs": bear_signs,
                        "source": "holding" if tk in holdings else ("watchlist" if tk in watch else "scan"),
                        "opt": options_note(t, implied_vol(tk, a.offline_dir)) if sig in ("GREEN", "AMBER") else ""})
        print(f"  {tk}: {sig} {'*' * n_stars} ({t['score']}/5 daily, weekly {'ok' if t['weekly']['confirms'] else 'no'}, "
              f"smart {'yes' if smart and smart['confirms'] else 'no'})")

    # options playbook: one capped-risk idea per green / bearish stock
    import playbook
    acct = None
    if api is not None:
        try:
            acct = float(api.account()["equity"])
        except Exception:
            pass
    n_ideas = 0
    for r in results:
        r["option_idea"] = None
        if r["holding"] or not (r["signal"] == "GREEN" or r.get("bear_score", 0) >= 3):
            continue
        idea = playbook.suggest(r, acct, a.offline_dir)
        if idea and not idea.get("error"):
            r["option_idea"] = idea
            n_ideas += idea["meets_rule"]
            print(f"  OPTIONS {r['ticker']}: {idea['strategy']} {idea['reward_risk']:.1f}x"
                  f"{'' if idea['meets_rule'] else ' (below 2:1, shown as no-go)'}")

    state = load_state()
    changes = find_changes(results, state) if state is not None else []
    save_state(results)
    save_latest(results, a.mode, a.paper)

    extra = []
    if api is not None:
        actions = paper.execute(api, results)
        for x in actions:
            print("  PAPER " + x)
        extra = (["Paper trades:"] + actions) if actions else []
        extra.append(paper.performance(api, float(bench["Close"].iloc[-1]) if bench is not None else None))

    if a.mode == "daily":
        traded = api is not None and len(extra) > 1
        if state is None and not traded:
            print("First run: today's signals saved as the baseline. Alerts start from the next run.")
            return
        if not changes and not traded:
            print("No signal changes - nothing sent.")
            return
        for r in changes:
            print(f"  CHANGE {r['ticker']}: {r['prev']} -> {label(r)}")

    subject, html = build_email(results, a.mode, changes)
    OUT.mkdir(exist_ok=True)
    path = OUT / f"signals_{a.mode}_{date.today().isoformat()}.html"
    path.write_text(html, encoding="utf-8")
    print(f"Saved {path}")
    if not a.no_send:
        title, body, prio, tag = build_notification(results, a.mode, changes, extra)
        try:
            send_notification(title, body, prio, tag)
        except Exception as e:
            print(f"Notification failed: {e}")
        send_email(subject, html)   # only if email settings are filled in


if __name__ == "__main__":
    main()
