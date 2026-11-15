#!/usr/bin/env python3
"""
Value screener + thesis drafter (prototype)
-------------------------------------------
Steps 1-2 of the research agent:
  1. Pull annual financials for a list of US companies from SEC EDGAR (free).
  2. Compute growth, cash flow, debt trend and a simple DCF value range.
  3. Flag companies that pass your screen and write a one-page thesis draft for each.

Usage:
  python screener.py                         # uses watchlist.txt
  python screener.py --tickers AAPL MSFT KO  # ad-hoc list
  python screener.py --top 200               # largest ~200 US filers
  python screener.py --ai                    # also ask Claude to draft catalysts/risks
                                             # (needs ANTHROPIC_API_KEY set)

Share prices: uses yfinance if installed; otherwise reads prices.csv (ticker,price).

This is a research aid, not investment advice. Check every number against the
filing before acting on it.
"""
import argparse, csv, json, os, sys, time
from datetime import date, datetime
from pathlib import Path

import requests

# --------------------------------------------------------------------------------------
# SETTINGS - edit these to change the screen and the valuation assumptions
# --------------------------------------------------------------------------------------
# SEC requires a User-Agent with a name and email. Put your own details here.
USER_AGENT = "Julien Kabongo research julien@camdenacquisitionhub.com"

SCREEN = {
    "min_revenue_growth": 0.05,   # latest year revenue growth >= 5%
    "min_positive_fcf_years": 3,  # free cash flow positive in each of the last 3 years
    "min_margin_of_safety": 0.25, # base-case value at least 25% above price
    "max_share_dilution": 0.03,   # share count up no more than 3% a year
}

DCF = {
    "discount_rate": 0.10,   # required annual return
    "terminal_growth": 0.025,
    "max_growth": 0.12,      # cap on assumed growth, however fast history was
    "years_high": 5,         # years at the assumed growth rate
    "years_fade": 5,         # years fading down to terminal growth
}

# XBRL tags to try, in order (companies use different ones)
TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "op_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
              "PaymentsForCapitalImprovements", "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
    "debt_total": ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount",
                   "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
                   "DebtAndCapitalLeaseObligations"],          # include current portion
    "debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_current": ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "short_borrowings": ["ShortTermBorrowings", "CommercialPaper"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
FLOW_ITEMS = {"revenue", "net_income", "op_cash_flow", "capex", "shares"}

HERE = Path(__file__).parent
OUT = HERE / "output"


# --------------------------------------------------------------------------------------
# DATA
# --------------------------------------------------------------------------------------
class Edgar:
    def __init__(self, offline_dir=None):
        self.offline_dir = Path(offline_dir) if offline_dir else None
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self._tickers = None

    def _get(self, url):
        time.sleep(0.12)  # SEC limit is 10 requests/second
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def tickers(self):
        if self._tickers is None:
            if self.offline_dir:
                data = json.loads((self.offline_dir / "company_tickers.json").read_text())
            else:
                data = self._get("https://www.sec.gov/files/company_tickers.json")
            self._tickers = [(v["ticker"].upper(), int(v["cik_str"]), v["title"])
                             for v in data.values()]
        return self._tickers

    def lookup(self, ticker):
        for t, cik, name in self.tickers():
            if t == ticker.upper():
                return cik, name
        return None, None

    def facts(self, cik):
        if self.offline_dir:
            return json.loads((self.offline_dir / f"CIK{cik:010d}.json").read_text())
        return self._get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")


def _days(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def _tag_series(gaap, tag, item):
    units = gaap[tag]["units"]
    rows = units.get("USD") or units.get("shares") or []
    out = {}
    for r in rows:
        if r.get("form") not in ("10-K", "10-K/A"):
            continue
        if item in FLOW_ITEMS:
            if "start" not in r or not (350 <= _days(r["start"], r["end"]) <= 380):
                continue
        prev = out.get(r["end"])
        if prev is None or r["filed"] > prev[1]:
            out[r["end"]] = (r["val"], r["filed"])
    return {k: v[0] for k, v in out.items()}


def annual_series(facts, item, sources=None):
    """{fiscal_year_end: value} from 10-K filings, merged across all the tags a company
    has used over time (companies switch tags, e.g. 'Revenues' -> 'RevenueFromContract...').
    For each year the highest-priority tag wins. `sources` records which tag fed each year."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    merged, used = {}, {}
    for tag in reversed(TAGS[item]):          # lowest priority first, higher ones overwrite
        if tag in gaap:
            ser = _tag_series(gaap, tag, item)
            merged.update(ser)
            used.update({k: tag for k in ser})
    if sources is not None:
        sources[item] = used
    return dict(sorted(merged.items()))


def last_n(series, n):
    return [series[k] for k in sorted(series)[-n:]]


# --------------------------------------------------------------------------------------
# ANALYSIS
# --------------------------------------------------------------------------------------
def analyse(ticker, name, facts):
    src = {}
    s = {k: annual_series(facts, k, src) for k in TAGS}
    years = sorted(s["revenue"])[-5:]
    if len(years) < 3:
        return None
    notes = []

    def at(item, y):
        return s[item].get(y)

    rows = []
    for y in years:
        ocf, capex = at("op_cash_flow", y), at("capex", y)
        if ocf is not None and capex is None:
            notes.append(f"{y}: capital spending not found, so free cash flow is left blank")
        # debt: take the LARGEST of the ways companies report it (understating debt overvalues)
        parts = (at("debt_noncurrent", y) or 0) + (at("debt_current", y) or 0) + (at("short_borrowings", y) or 0)
        debt = max(at("debt_total", y) or 0, parts)
        rows.append({
            "year_end": y,
            "revenue": at("revenue", y),
            "net_income": at("net_income", y),
            "fcf": (ocf - capex) if (ocf is not None and capex is not None) else None,
            "debt": debt,
            "cash": at("cash", y),
            "shares": at("shares", y),
        })

    rev = [r["revenue"] for r in rows]
    fcf = [r["fcf"] for r in rows if r["fcf"] is not None]
    latest = rows[-1]

    m = {"ticker": ticker, "name": name, "history": rows,
         "latest_year_end": latest["year_end"], "data_notes": notes,
         "tags_used": {k: src.get(k, {}).get(latest["year_end"]) for k in ("revenue", "op_cash_flow", "capex", "debt_total", "debt_noncurrent")}}
    age = (date.today() - date.fromisoformat(latest["year_end"])).days
    m["stale"] = age > 550
    if m["stale"]:
        notes.append(f"latest annual report is {age // 30} months old")
    m["revenue_growth"] = rev[-1] / rev[-2] - 1 if rev[-2] else None
    n = len(rev) - 1
    m["revenue_cagr"] = (rev[-1] / rev[0]) ** (1 / n) - 1 if rev[0] and rev[-1] > 0 else None
    m["net_margin"] = latest["net_income"] / latest["revenue"] if latest["net_income"] is not None else None
    m["fcf_latest"] = fcf[-1] if fcf else None
    m["fcf_avg3"] = sum(fcf[-3:]) / len(fcf[-3:]) if len(fcf) >= 3 else None
    m["positive_fcf_years"] = sum(1 for x in fcf[-3:] if x > 0)
    m["fcf_improving"] = len(fcf) >= 3 and fcf[-1] > fcf[-3]
    debts = [r["debt"] for r in rows]
    m["debt_latest"] = debts[-1]
    m["debt_declining"] = len(debts) >= 3 and debts[-1] < debts[-3]
    sh = [r["shares"] for r in rows if r["shares"]]
    m["shares_latest"] = sh[-1] if sh else None
    m["share_change_pa"] = (sh[-1] / sh[0]) ** (1 / (len(sh) - 1)) - 1 if len(sh) >= 2 else None
    m["cash_latest"] = latest["cash"] or 0
    return m


def dcf_per_share(m, growth):
    if not m["fcf_avg3"] or m["fcf_avg3"] <= 0 or not m["shares_latest"]:
        return None
    r, tg = DCF["discount_rate"], DCF["terminal_growth"]
    fcf, pv, t = m["fcf_avg3"], 0.0, 0
    for _ in range(DCF["years_high"]):
        t += 1; fcf *= 1 + growth; pv += fcf / (1 + r) ** t
    for i in range(1, DCF["years_fade"] + 1):
        g = growth + (tg - growth) * i / DCF["years_fade"]
        t += 1; fcf *= 1 + g; pv += fcf / (1 + r) ** t
    terminal = fcf * (1 + tg) / (r - tg) / (1 + r) ** t
    equity = pv + terminal - m["debt_latest"] + m["cash_latest"]
    return equity / m["shares_latest"]


def value(m):
    base_g = max(0.0, min(m["revenue_cagr"] or 0, DCF["max_growth"]))
    m["growth_assumed"] = base_g
    m["iv_bear"] = dcf_per_share(m, base_g * 0.5)
    m["iv_base"] = dcf_per_share(m, base_g)
    m["iv_bull"] = dcf_per_share(m, min(base_g * 1.5, DCF["max_growth"] * 1.25))


def screen(m):
    reasons, flags = [], []
    if m["revenue_growth"] is None or m["revenue_growth"] < SCREEN["min_revenue_growth"]:
        reasons.append("revenue growth below threshold")
    if m["positive_fcf_years"] < SCREEN["min_positive_fcf_years"]:
        reasons.append("free cash flow not consistently positive")
    if not (m["fcf_improving"] or m["debt_declining"]):
        reasons.append("neither FCF improving nor debt falling")
    if m.get("price") and m.get("iv_base"):
        m["margin_of_safety"] = m["iv_base"] / m["price"] - 1
        if m["margin_of_safety"] < SCREEN["min_margin_of_safety"]:
            reasons.append("not cheap enough vs base-case value")
    else:
        m["margin_of_safety"] = None
        reasons.append("no price or no valuation")
    if m["share_change_pa"] and m["share_change_pa"] > SCREEN["max_share_dilution"]:
        reasons.append("share count rising (dilution)")
    if m.get("stale"):
        reasons.append("financial data out of date")
    for n in m.get("data_notes", []):
        flags.append("Data: " + n + ".")
    # red flags shown in the thesis even if it passes
    if m["net_margin"] is not None and m["net_margin"] < 0:
        flags.append("Loss-making on a net income basis in the latest year.")
    last = m["history"][-1]
    if last["net_income"] and m["fcf_latest"] and m["fcf_latest"] < 0.5 * last["net_income"]:
        flags.append("Free cash flow is well below reported profit - check working capital and capex.")
    if m["debt_latest"] and m["fcf_avg3"] and m["fcf_avg3"] > 0 and m["debt_latest"] / m["fcf_avg3"] > 5:
        flags.append(f"Debt is {m['debt_latest']/m['fcf_avg3']:.1f}x average free cash flow.")
    if m["iv_base"] and m["iv_bear"] and m.get("price") and m["iv_bear"] < m["price"]:
        flags.append("Bear case is below today's price - the thesis depends on growth continuing.")
    m["fail_reasons"], m["red_flags"] = reasons, flags
    m["passes"] = not reasons
    return m


# --------------------------------------------------------------------------------------
# PRICES
# --------------------------------------------------------------------------------------
def load_prices(tickers):
    prices = {}
    p = HERE / "prices.csv"
    if p.exists():
        with p.open() as f:
            for row in csv.DictReader(l for l in f if not l.startswith("#")):
                try:
                    prices[row["ticker"].upper()] = float(row["price"])
                except (TypeError, ValueError, AttributeError):
                    pass
    missing = [t for t in tickers if t not in prices]
    if missing:
        import prices as px   # Alpaca data if keys are set, else Yahoo Finance
        for t in missing:
            try:
                p = px.latest(t)
                if p:
                    prices[t] = p
            except Exception:
                pass
    return prices


# --------------------------------------------------------------------------------------
# OUTPUT
# --------------------------------------------------------------------------------------
def money(x):
    if x is None: return "n/a"
    a = abs(x)
    if a >= 1e9: return f"${x/1e9:,.2f}bn"
    if a >= 1e6: return f"${x/1e6:,.1f}m"
    return f"${x:,.0f}"

def pct(x):
    return "n/a" if x is None else f"{x*100:.1f}%"

def ps(x):
    return "n/a" if x is None else f"${x:,.2f}"


def shares_fmt(x):
    return "n/a" if not x else f"{x/1e6:,.1f}m"


def thesis_md(m, ai_text=None):
    h = m["history"]
    table = "| Year end | Revenue | Net income | Free cash flow | Debt | Diluted shares |\n|---|---|---|---|---|---|\n"
    for r in h:
        table += (f"| {r['year_end']} | {money(r['revenue'])} | {money(r['net_income'])} | "
                  f"{money(r['fcf'])} | {money(r['debt'])} | "
                  f"{shares_fmt(r['shares'])} |\n")
    verdict = "PASSES the screen" if m["passes"] else "Does not pass: " + "; ".join(m["fail_reasons"])
    flags = "\n".join(f"- {f}" for f in m["red_flags"]) or "- None flagged automatically."
    qual = ai_text or (
        "_To fill in (by you, or run with `--ai`):_\n\n"
        "- **What the business does and why it earns money:**\n"
        "- **Why the market might be mispricing it:**\n"
        "- **Catalyst and expected timing:** (earnings date, refinancing, contract, spin-off...)\n"
        "- **What would prove the thesis wrong:**\n"
        "- **Options idea (only if a catalyst has a date):** structure, strike, expiry, max loss\n")
    return f"""# {m['name']} ({m['ticker']}) - thesis draft

_Generated {date.today().isoformat()} from SEC 10-K data (latest fiscal year end {m['latest_year_end']}). Draft only - verify against the filings._

**Screen result:** {verdict}

## Price vs value
| | Per share |
|---|---|
| Current price | {ps(m.get('price'))} |
| Bear case (growth {pct(m['growth_assumed']*0.5)}) | {ps(m['iv_bear'])} |
| **Base case (growth {pct(m['growth_assumed'])})** | **{ps(m['iv_base'])}** |
| Bull case | {ps(m['iv_bull'])} |
| Margin of safety (base) | {pct(m['margin_of_safety'])} |

DCF on 3-year average free cash flow, {pct(DCF['discount_rate'])} discount rate, {pct(DCF['terminal_growth'])} terminal growth, net debt deducted.

## Key numbers
- Revenue growth, latest year: **{pct(m['revenue_growth'])}** (compound annual over the period: {pct(m['revenue_cagr'])})
- Net margin: {pct(m['net_margin'])}
- Free cash flow, latest: {money(m['fcf_latest'])} | 3-year average: {money(m['fcf_avg3'])} | improving: {'yes' if m['fcf_improving'] else 'no'}
- Debt: {money(m['debt_latest'])} | falling vs 2 years ago: {'yes' if m['debt_declining'] else 'no'}
- Share count change per year: {pct(m['share_change_pa'])}

{table}
## Red flags
{flags}

<sub>SEC tags used for {m['latest_year_end']}: {", ".join(f"{k}={v}" for k, v in (m.get("tags_used") or {}).items() if v)}</sub>

## Thesis
{qual}
"""


def ai_draft(m):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    numbers = json.dumps({k: v for k, v in m.items() if k not in ("fail_reasons",)}, default=str)
    prompt = (
        "You are helping an independent investor draft an equity research thesis. "
        "Using ONLY the figures below plus your general knowledge of the company, write short bullet "
        "answers for: what the business does; why the market might be mispricing it; possible catalysts "
        "with rough timing; what would prove the thesis wrong. Say clearly where you are unsure or where "
        "your knowledge may be out of date. Do not invent figures and do not recommend a trade.\n\n" + numbers)
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
                  "max_tokens": 900, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "_Drafted by Claude - check before relying on it._\n\n" + r.json()["content"][0]["text"]
    except Exception as e:
        print(f"  AI draft failed for {m['ticker']}: {e}")
        return None


# --------------------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--top", type=int, help="screen the N largest SEC filers")
    ap.add_argument("--ai", action="store_true", help="draft catalysts/risks with Claude API")
    ap.add_argument("--all-theses", action="store_true", help="write a thesis for every company, not just passes")
    ap.add_argument("--offline-dir", help=argparse.SUPPRESS)  # for testing with saved JSON
    a = ap.parse_args()

    ed = Edgar(a.offline_dir)
    if a.tickers:
        tickers = [t.upper() for t in a.tickers]
    elif a.top:
        tickers = [t for t, _, _ in ed.tickers()[: a.top]]
    else:
        wl = HERE / "watchlist.txt"
        tickers = [l.strip().upper() for l in wl.read_text().splitlines()
                   if l.strip() and not l.startswith("#")]

    print(f"Screening {len(tickers)} companies...")
    prices = load_prices(tickers)
    results = []
    for t in tickers:
        cik, name = ed.lookup(t)
        if not cik:
            print(f"  {t}: not found in SEC list"); continue
        try:
            m = analyse(t, name, ed.facts(cik))
        except Exception as e:
            print(f"  {t}: data error ({e})"); continue
        if not m:
            print(f"  {t}: not enough annual data"); continue
        m["price"] = prices.get(t)
        value(m); screen(m)
        results.append(m)
        print(f"  {t}: {'PASS' if m['passes'] else 'fail'}"
              f"  price {ps(m['price'])}  base value {ps(m['iv_base'])}")

    OUT.mkdir(exist_ok=True)
    (OUT / "theses").mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    with (OUT / f"screen_{stamp}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "passes", "price", "value_bear", "value_base", "value_bull",
                    "margin_of_safety", "revenue_growth", "revenue_cagr", "net_margin",
                    "fcf_latest", "fcf_avg3", "fcf_improving", "debt_latest", "debt_declining",
                    "share_change_pa", "why_not"])
        for m in sorted(results, key=lambda x: -(x["margin_of_safety"] or -9)):
            w.writerow([m["ticker"], m["name"], m["passes"], m["price"], m["iv_bear"], m["iv_base"],
                        m["iv_bull"], m["margin_of_safety"], m["revenue_growth"], m["revenue_cagr"],
                        m["net_margin"], m["fcf_latest"], m["fcf_avg3"], m["fcf_improving"],
                        m["debt_latest"], m["debt_declining"], m["share_change_pa"],
                        "; ".join(m["fail_reasons"])])

    chosen = results if a.all_theses else [m for m in results if m["passes"]]
    for m in chosen:
        ai = ai_draft(m) if a.ai else None
        (OUT / "theses" / f"{m['ticker']}_{stamp}.md").write_text(thesis_md(m, ai))

    passed = sum(m["passes"] for m in results)
    print(f"\nDone: {passed} of {len(results)} passed. "
          f"Results in {OUT/f'screen_{stamp}.csv'}, theses in {OUT/'theses'}")


if __name__ == "__main__":
    main()
