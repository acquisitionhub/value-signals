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
  python screener.py --top 500               # scan the ~500 largest US companies + your watchlist
  python screener.py --ai                    # also ask Claude to draft catalysts/risks
                                             # (needs ANTHROPIC_API_KEY set)

Share prices: uses yfinance if installed; otherwise reads prices.csv (ticker,price).

This is a research aid, not investment advice. Check every number against the
filing before acting on it.
"""
import argparse, csv, json, os, re, sys, time
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
    # quality rules (only applied when the data exists; missing data never fails a company)
    "min_roic": 0.10,             # return on invested capital >= 10%
    "min_cash_conversion": 0.60,  # 3-yr free cash flow >= 60% of 3-yr net profit
    "require_eps_growth": True,   # diluted EPS higher than ~2 years earlier
    # forensic rules (skipped when the data is missing)
    "max_beneish": -1.78,         # Beneish M-Score above this = likely earnings manipulator
    "min_piotroski": 4,           # Piotroski F-Score of 3 or less (out of 7+ known checks) fails
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
    # ---- quality pack ----
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
    "gross_profit": ["GrossProfit"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "op_income": ["OperatingIncomeLoss"],
    "interest": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
                 "InterestPaidNet", "InterestExpenseNet"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "assets": ["Assets"],
    "goodwill": ["Goodwill"],
    "retained": ["RetainedEarningsAccumulatedDeficit"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    # ---- forensic pack ----
    "receivables": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent", "AccountsNotesAndLoansReceivableNetCurrent"],
    "sga": ["SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"],
    "depreciation": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "Depreciation",
                     "DepreciationAmortizationAndAccretionNet"],
    "ppe": ["PropertyPlantAndEquipmentNet",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"],
    "liabilities": ["Liabilities"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
FLOW_ITEMS = {"revenue", "net_income", "op_cash_flow", "capex", "shares",
              "eps", "gross_profit", "cost_of_revenue", "op_income", "interest", "tax", "pretax",
              "buybacks", "dividends", "sbc", "sga", "depreciation"}

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

    def industry(self, cik):
        """(SIC code, description) from SEC. Cached forever: companies rarely change industry."""
        if self.offline_dir:
            p = self.offline_dir / f"submissions_CIK{cik:010d}.json"
            d = json.loads(p.read_text()) if p.exists() else {}
            return (int(d["sic"]) if str(d.get("sic", "")).isdigit() else None), d.get("sicDescription", "")
        cp = HERE / "cache" / "industry.json"
        cache = json.loads(cp.read_text()) if cp.exists() else {}
        if str(cik) not in cache:
            try:
                d = self._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
                cache[str(cik)] = [int(d["sic"]) if str(d.get("sic", "")).isdigit() else None,
                                   d.get("sicDescription", "")]
                cp.parent.mkdir(exist_ok=True)
                cp.write_text(json.dumps(cache))
            except Exception:
                return None, ""
        return tuple(cache[str(cik)])


def is_financial(sic):
    """Banks, insurers, brokers, REITs and other finance/real-estate (SIC 6000-6799):
    a free-cash-flow valuation doesn't fit how these businesses work."""
    return sic is not None and 6000 <= sic <= 6799


def read_watchlist():
    wl = HERE / "watchlist.txt"
    if not wl.exists():
        return []
    return list(dict.fromkeys(l.strip().upper() for l in wl.read_text().splitlines()
                              if l.strip() and not l.startswith("#")))


def _days(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def _tag_series(gaap, tag, item):
    units = gaap[tag]["units"]
    rows = units.get("USD") or units.get("USD/shares") or units.get("shares") or []
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
            # quality pack
            "eps": at("eps", y),
            "gross_profit": at("gross_profit", y) if at("gross_profit", y) is not None else (
                (at("revenue", y) - at("cost_of_revenue", y))
                if at("revenue", y) is not None and at("cost_of_revenue", y) is not None else None),
            "op_income": at("op_income", y), "interest": at("interest", y),
            "tax": at("tax", y), "pretax": at("pretax", y),
            "current_assets": at("current_assets", y), "current_liabilities": at("current_liabilities", y),
            "equity": at("equity", y), "assets": at("assets", y), "goodwill": at("goodwill", y),
            "retained": at("retained", y), "buybacks": at("buybacks", y),
            "dividends": at("dividends", y), "sbc": at("sbc", y),
            "ocf": at("op_cash_flow", y), "receivables": at("receivables", y), "sga": at("sga", y),
            "depreciation": at("depreciation", y), "ppe": at("ppe", y), "liabilities": at("liabilities", y),
        })

    # stock split / reverse split since the last annual report: the 10-K share count is stale but the
    # price isn't. Compare with the latest cover-page share count (filed with every 10-Q and 10-K).
    dei = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
    cover = [r for r in (dei.get("units", {}).get("shares") or []) if r.get("val")]
    last_sh = rows[-1]["shares"]
    if cover and last_sh:
        # companies with several share classes (e.g. Class A + Class B) report one cover-page line per
        # class in the same filing: add them up before comparing
        newest = max(cover, key=lambda r: (r.get("filed", ""), r.get("end", "")))
        same = [r for r in cover if r.get("accn") == newest.get("accn") and r.get("end") == newest.get("end")] \
            if newest.get("accn") else [r for r in cover if (r.get("filed"), r.get("end")) == (newest.get("filed"), newest.get("end"))]
        total_cover = sum(r["val"] for r in same)
        ratio = total_cover / last_sh
        factor = None
        if ratio > 50 or ratio < 0.02:
            notes.append(f"cover-page share count looks inconsistent ({ratio:.3g}x the annual figure): ignored")
        elif ratio >= 1.5:
            factor = float(round(ratio))
        elif ratio <= 1 / 1.5:
            factor = 1 / round(1 / ratio)
        if factor:
            for r in rows:
                if r["shares"]:
                    r["shares"] *= factor
                if r.get("eps"):
                    r["eps"] /= factor
            label = f"{factor:g}-for-1 split" if factor > 1 else f"1-for-{1/factor:g} reverse split"
            notes.append(f"share count changed about {ratio:.1f}x since the last annual report "
                         f"({label}, filed {newest.get('filed')}): per-share figures adjusted")

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
    quality(m)
    forensic(m)
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


def _cagr(a, b, years):
    if a is None or b is None or a <= 0 or b <= 0 or years <= 0:
        return None
    return (b / a) ** (1 / years) - 1


def quality(m):
    """Quality pack from all four statements. Every metric is None when the data is missing."""
    h = m["history"]
    last, first = h[-1], h[max(0, len(h) - 3)]           # compare with ~2 years earlier
    yrs = len(h) - 1 - max(0, len(h) - 3)
    pct = lambda a, b: (a / b) if (a is not None and b not in (None, 0)) else None
    q = {}
    # income statement
    q["eps"] = last["eps"]
    q["eps_cagr"] = _cagr(first["eps"], last["eps"], yrs)
    q["rev_cagr2"] = _cagr(first["revenue"], last["revenue"], yrs)
    q["gross_margin"] = pct(last["gross_profit"], last["revenue"])
    q["gross_margin_prev"] = pct(first["gross_profit"], first["revenue"])
    q["op_margin"] = pct(last["op_income"], last["revenue"])
    q["op_margin_prev"] = pct(first["op_income"], first["revenue"])
    q["interest_cover"] = (last["op_income"] / last["interest"]) if (last["op_income"] is not None and last["interest"]) else None
    # balance sheet
    q["current_ratio"] = pct(last["current_assets"], last["current_liabilities"])
    q["goodwill_share"] = pct(last["goodwill"], last["assets"])
    tax_rate = 0.21
    if last["tax"] is not None and last["pretax"] and last["pretax"] > 0:
        tax_rate = min(max(last["tax"] / last["pretax"], 0.0), 0.35)
    invested = None
    if last["equity"] is not None:
        invested = last["equity"] + (last["debt"] or 0) - (last["cash"] or 0)
    q["roic"] = ((last["op_income"] * (1 - tax_rate)) / invested) if (last["op_income"] is not None and invested and invested > 0) else None
    q["roe"] = pct(last["net_income"], last["equity"]) if (last["equity"] or 0) > 0 else None
    # cash flow statement
    fcf3 = [r["fcf"] for r in h[-3:] if r["fcf"] is not None]
    ni3 = [r["net_income"] for r in h[-3:] if r["net_income"] is not None]
    q["cash_conversion"] = (sum(fcf3) / sum(ni3)) if (len(fcf3) == 3 and len(ni3) == 3 and sum(ni3) > 0) else None
    q["sbc_share"] = pct(last["sbc"], last["revenue"])
    paid = (last["buybacks"] or 0) + (last["dividends"] or 0)
    q["payout_of_fcf"] = (paid / last["fcf"]) if (last["fcf"] and last["fcf"] > 0 and paid) else None
    q["buybacks"], q["dividends"] = last["buybacks"], last["dividends"]
    # retained earnings
    q["retained"] = last["retained"]
    q["retained_growing"] = (last["retained"] > first["retained"]) if (last["retained"] is not None and first["retained"] is not None) else None

    # growth quality: revenue vs EPS
    rg, eg = q["rev_cagr2"], q["eps_cagr"]
    if rg is not None and eg is not None:
        if rg < 0 and eg > 0:
            q["growth_quality"] = "EPS rising while revenue falls: cost cuts, one-offs or buybacks"
        elif eg > rg + 0.03 and rg > 0:
            q["growth_quality"] = "EPS growing faster than revenue: margins or buybacks improving"
        elif rg > 0 and eg < rg - 0.05:
            q["growth_quality"] = "revenue growing faster than EPS: costs rising"
        else:
            q["growth_quality"] = "EPS and revenue growing together"
    else:
        q["growth_quality"] = None

    # score: 6 checks, only counted when data exists
    checks = {
        "Return on capital >= 10%": None if q["roic"] is None else q["roic"] >= 0.10,
        "Cash conversion >= 70%": None if q["cash_conversion"] is None else q["cash_conversion"] >= 0.70,
        "EPS growing with revenue": None if (eg is None or rg is None) else (eg > 0 and rg > 0),
        "Interest cover >= 3x": (True if (q["interest_cover"] is None and (last["debt"] or 0) == 0)
                                  else None if q["interest_cover"] is None else q["interest_cover"] >= 3),
        "Current ratio >= 1": None if q["current_ratio"] is None else q["current_ratio"] >= 1,
        "Retained earnings growing": q["retained_growing"],
    }
    q["checks"] = checks
    known = [v for v in checks.values() if v is not None]
    q["score"], q["score_of"] = sum(known), len(known)
    m["quality"] = q
    return q


def _div(a, b):
    return (a / b) if (a is not None and b not in (None, 0)) else None


def forensic(m):
    """Beneish M-Score, Piotroski F-Score, accruals ratio. Altman Z needs the price, so it's
    finished in screen(). Every score is None when the filings don't have enough data."""
    h = m["history"]
    f = {"beneish": None, "beneish_missing": None, "piotroski": None, "piotroski_of": 0,
         "piotroski_checks": {}, "accruals": None, "altman": None, "altman_zone": None}
    if len(h) < 2:
        m["forensic"] = f
        return f
    t, p = h[-1], h[-2]

    # ---- Beneish M-Score (8 variables). Missing parts are set to the neutral value (1, or 0 for TATA)
    neutral = 0
    def idx(v):
        nonlocal neutral
        if v is None:
            neutral += 1
            return 1.0
        return v
    s_t, s_p = t["revenue"], p["revenue"]
    if s_t and s_p and t["assets"] and t["net_income"] is not None and t["ocf"] is not None:
        dsri = idx(_div(_div(t["receivables"], s_t), _div(p["receivables"], s_p)))
        gm_t, gm_p = _div(t["gross_profit"], s_t), _div(p["gross_profit"], s_p)
        gmi = idx(_div(gm_p, gm_t) if (gm_t and gm_t > 0 and gm_p) else None)
        def aq(r):
            if r["assets"] and r["current_assets"] is not None and r["ppe"] is not None:
                return 1 - (r["current_assets"] + r["ppe"]) / r["assets"]
            return None
        aqi = idx(_div(aq(t), aq(p)) if (aq(p) not in (None, 0)) else None)
        sgi = s_t / s_p
        def dr(r):
            if r["depreciation"] is not None and r["ppe"] is not None and (r["depreciation"] + r["ppe"]) > 0:
                return r["depreciation"] / (r["depreciation"] + r["ppe"])
            return None
        depi = idx(_div(dr(p), dr(t)))
        sgai = idx(_div(_div(t["sga"], s_t), _div(p["sga"], s_p)))
        tata = (t["net_income"] - t["ocf"]) / t["assets"]
        def lev(r):
            if r["assets"] and r["current_liabilities"] is not None:
                return (r["current_liabilities"] + (r["debt"] or 0)) / r["assets"]
            return None
        lvgi = idx(_div(lev(t), lev(p)))
        if neutral <= 3:
            f["beneish"] = (-4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi + 0.115 * depi
                            - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
            f["beneish_missing"] = neutral

    # ---- Piotroski F-Score (9 checks, only those with data are counted)
    roa = lambda r, prev: _div(r["net_income"], prev["assets"])
    c = {}
    if len(h) >= 3:
        pp = h[-3]
        r_t, r_p = roa(t, p), roa(p, pp)
    else:
        r_t, r_p = _div(t["net_income"], t["assets"]), None
    c["Positive return on assets"] = None if r_t is None else r_t > 0
    c["Positive operating cash flow"] = None if t["ocf"] is None else t["ocf"] > 0
    c["Return on assets improving"] = None if (r_t is None or r_p is None) else r_t > r_p
    c["Cash flow above profit"] = None if (t["ocf"] is None or t["net_income"] is None) else t["ocf"] > t["net_income"]
    lv_t, lv_p = _div(t["debt"], t["assets"]), _div(p["debt"], p["assets"])
    c["Leverage falling"] = None if (lv_t is None or lv_p is None) else lv_t <= lv_p
    cr_t, cr_p = _div(t["current_assets"], t["current_liabilities"]), _div(p["current_assets"], p["current_liabilities"])
    c["Current ratio improving"] = None if (cr_t is None or cr_p is None) else cr_t > cr_p
    c["No new shares issued"] = None if (not t["shares"] or not p["shares"]) else t["shares"] <= p["shares"] * 1.005
    gm_t, gm_p = _div(t["gross_profit"], s_t), _div(p["gross_profit"], s_p)
    c["Gross margin improving"] = None if (gm_t is None or gm_p is None) else gm_t > gm_p
    at_t, at_p = _div(s_t, t["assets"]), _div(s_p, p["assets"])
    c["Asset turnover improving"] = None if (at_t is None or at_p is None) else at_t > at_p
    known = [v for v in c.values() if v is not None]
    f["piotroski_checks"], f["piotroski"], f["piotroski_of"] = c, sum(known), len(known)

    # ---- Sloan accruals ratio: profit not backed by cash, scaled by average assets
    if t["net_income"] is not None and t["ocf"] is not None and t["assets"]:
        avg_a = (t["assets"] + (p["assets"] or t["assets"])) / 2
        f["accruals"] = (t["net_income"] - t["ocf"]) / avg_a
    m["forensic"] = f
    return f


def altman(m):
    """Altman Z-Score using market value of equity (needs the price)."""
    f = m.get("forensic") or {}
    t = m["history"][-1]
    mve = (m["price"] * m["shares_latest"]) if (m.get("price") and m.get("shares_latest")) else None
    ta, tl = t["assets"], t["liabilities"]
    if ta and tl and mve and t["current_assets"] is not None and t["current_liabilities"] is not None \
            and t["retained"] is not None and t["op_income"] is not None and t["revenue"]:
        z = (1.2 * (t["current_assets"] - t["current_liabilities"]) / ta + 1.4 * t["retained"] / ta
             + 3.3 * t["op_income"] / ta + 0.6 * mve / tl + 1.0 * t["revenue"] / ta)
        f["altman"] = z
        f["altman_zone"] = "safe" if z > 2.99 else "grey" if z >= 1.81 else "distress"
    return f


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
    q = m.get("quality") or {}
    if q.get("roic") is not None and q["roic"] < SCREEN["min_roic"]:
        reasons.append(f"return on capital only {q['roic']*100:.0f}%")
    if q.get("cash_conversion") is not None and q["cash_conversion"] < SCREEN["min_cash_conversion"]:
        reasons.append(f"cash conversion only {q['cash_conversion']*100:.0f}% of profit")
    if SCREEN["require_eps_growth"] and q.get("eps_cagr") is not None and q["eps_cagr"] <= 0:
        reasons.append("earnings per share not growing")
    q["pe"] = (m["price"] / q["eps"]) if (m.get("price") and q.get("eps") and q["eps"] > 0) else None
    fz = altman(m) if m.get("forensic") is not None else {}
    if fz.get("beneish") is not None and fz["beneish"] > SCREEN["max_beneish"]:
        reasons.append(f"possible earnings manipulation (Beneish M-Score {fz['beneish']:.2f})")
    if fz.get("altman_zone") == "distress":
        reasons.append(f"Altman Z-Score {fz['altman']:.2f}: financial distress zone")
    if fz.get("piotroski_of", 0) >= 7 and fz["piotroski"] <= SCREEN["min_piotroski"] - 1:
        reasons.append(f"weak financial health (Piotroski {fz['piotroski']}/{fz['piotroski_of']})")
    if m.get("financial"):
        reasons.insert(0, f"bank/insurer/REIT ({m.get('industry') or 'financial'}): this valuation method doesn't fit")
    # sanity net: values this far from the market price almost always mean a data problem, not a bargain
    if m.get("price") and m.get("iv_base") and m["iv_base"] > 8 * m["price"]:
        reasons.insert(0, f"valuation looks implausible ({m['iv_base']/m['price']:.0f}x the price): likely a data problem, check the filing")
    pe_ = (m.get("quality") or {}).get("pe")
    if pe_ is not None and pe_ < 2:
        reasons.insert(0, f"P/E of {pe_:.1f} looks implausible: likely a share-count data problem")
    for n in m.get("data_notes", []):
        flags.append("Data: " + n + ".")
    # red flags shown in the thesis even if it passes
    if m["net_margin"] is not None and m["net_margin"] < 0:
        flags.append("Loss-making on a net income basis in the latest year.")
    if m["debt_latest"] and m["fcf_avg3"] and m["fcf_avg3"] > 0 and m["debt_latest"] / m["fcf_avg3"] > 5:
        flags.append(f"Debt is {m['debt_latest']/m['fcf_avg3']:.1f}x average free cash flow.")
    if m["iv_base"] and m["iv_bear"] and m.get("price") and m["iv_bear"] < m["price"]:
        flags.append("Bear case is below today's price - the thesis depends on growth continuing.")
    if q.get("growth_quality", "") and q["growth_quality"].startswith("EPS rising while revenue falls"):
        flags.append("EPS rising while revenue falls: profit growth may not last.")
    if q.get("cash_conversion") is not None and q["cash_conversion"] < 0.7:
        flags.append(f"Only {q['cash_conversion']*100:.0f}% of reported profit turned into free cash flow over 3 years.")
    if q.get("interest_cover") is not None and q["interest_cover"] < 3:
        flags.append(f"Interest cover only {q['interest_cover']:.1f}x: debt costs are heavy.")
    if q.get("current_ratio") is not None and q["current_ratio"] < 1:
        flags.append(f"Current ratio {q['current_ratio']:.2f}: short-term debts exceed short-term assets.")
    if q.get("goodwill_share") is not None and q["goodwill_share"] > 0.4:
        flags.append(f"Goodwill is {q['goodwill_share']*100:.0f}% of assets: growth bought through acquisitions; write-down risk.")
    if q.get("retained") is not None and q["retained"] < 0:
        flags.append("Negative retained earnings: past losses, or buybacks/dividends larger than profits.")
    if q.get("payout_of_fcf") is not None and q["payout_of_fcf"] > 1.2:
        flags.append(f"Paying out {q['payout_of_fcf']*100:.0f}% of free cash flow in buybacks and dividends: funded by debt or cash reserves.")
    if q.get("sbc_share") is not None and q["sbc_share"] > 0.08:
        flags.append(f"Share-based pay is {q['sbc_share']*100:.0f}% of revenue: dilutes shareholders.")
    if fz.get("beneish") is not None and -2.22 < fz["beneish"] <= SCREEN["max_beneish"]:
        flags.append(f"Beneish M-Score {fz['beneish']:.2f} is in the grey zone (-2.22 to -1.78): check receivables and accruals.")
    if fz.get("altman_zone") == "grey":
        flags.append(f"Altman Z-Score {fz['altman']:.2f} is in the grey zone (1.81 to 2.99).")
    if fz.get("accruals") is not None and fz["accruals"] > 0.10:
        flags.append(f"High accruals ({fz['accruals']*100:.0f}% of assets): profit far ahead of cash.")
    for name, cur, prev in (("Gross", q.get("gross_margin"), q.get("gross_margin_prev")),
                            ("Operating", q.get("op_margin"), q.get("op_margin_prev"))):
        if cur is not None and prev is not None and cur < prev - 0.03:
            flags.append(f"{name} margin fell from {prev*100:.1f}% to {cur*100:.1f}% in two years.")
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
        try:
            prices.update(px.latest_many(missing))
        except Exception as e:
            print(f"  price lookup problem: {e}")
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


def _ftable(m):
    f = m.get("forensic") or {}
    b = f.get("beneish")
    bz = "n/a" if b is None else ("likely manipulator" if b > -1.78 else "grey zone" if b > -2.22 else "unlikely manipulator")
    z = f.get("altman")
    pc = "\n".join(f"- {'✅' if v else '❌' if v is False else '➖'} {k}" for k, v in (f.get("piotroski_checks") or {}).items())
    return f"""| Score | Value | Reading |
|---|---|---|
| Beneish M-Score (earnings manipulation) | {"n/a" if b is None else f"{b:.2f}"} | {bz} (above -1.78 = red flag) |
| Piotroski F-Score (financial health) | {f.get("piotroski", "n/a")}/{f.get("piotroski_of", 0)} | {"n/a" if not f.get("piotroski_of") else "strong" if f["piotroski"] >= 0.75 * f["piotroski_of"] else "weak" if f["piotroski"] <= 0.35 * f["piotroski_of"] else "average"} |
| Altman Z-Score (bankruptcy risk) | {"n/a" if z is None else f"{z:.2f}"} | {f.get("altman_zone") or "n/a"} (below 1.81 = distress; less reliable for banks and asset-light firms) |
| Accruals ratio (profit not backed by cash) | {"n/a" if f.get("accruals") is None else f"{f['accruals']*100:.1f}%"} | {"n/a" if f.get("accruals") is None else "high" if f["accruals"] > 0.10 else "low" if f["accruals"] < 0 else "normal"} |

Piotroski checks:
{pc}"""


def _qscore(m):
    q = m.get("quality") or {}
    return f"{q.get('score', 0)}/{q.get('score_of', 0)} checks passed" if q.get("score_of") else "not enough data"


def _qtable(m):
    q = m.get("quality") or {}
    f = lambda x: "n/a" if x is None else f"{x*100:.1f}%"
    x = lambda v: "n/a" if v is None else f"{v:.1f}x"
    ticks = "\n".join(f"- {'✅' if v else '❌' if v is False else '➖'} {k}" for k, v in (q.get("checks") or {}).items())
    margin = lambda a, b: f"{f(a)} (was {f(b)})" if b is not None else f(a)
    return f"""{ticks}

| Statement | Measure | Value |
|---|---|---|
| Income | Diluted EPS (latest) | {"n/a" if q.get("eps") is None else f"${q['eps']:.2f}"} |
| Income | EPS growth vs revenue growth (per yr) | {f(q.get("eps_cagr"))} vs {f(q.get("rev_cagr2"))} |
| Income | Gross margin | {margin(q.get("gross_margin"), q.get("gross_margin_prev"))} |
| Income | Operating margin | {margin(q.get("op_margin"), q.get("op_margin_prev"))} |
| Income | Interest cover | {x(q.get("interest_cover"))} |
| Balance sheet | Return on invested capital | {f(q.get("roic"))} |
| Balance sheet | Return on equity | {f(q.get("roe"))} |
| Balance sheet | Current ratio | {"n/a" if q.get("current_ratio") is None else f"{q['current_ratio']:.2f}"} |
| Balance sheet | Goodwill share of assets | {f(q.get("goodwill_share"))} |
| Cash flow | Cash conversion (3-yr FCF / profit) | {f(q.get("cash_conversion"))} |
| Cash flow | Buybacks + dividends as % of FCF | {f(q.get("payout_of_fcf"))} |
| Cash flow | Share-based pay as % of revenue | {f(q.get("sbc_share"))} |
| Retained earnings | Latest / growing? | {money(q.get("retained"))} / {"n/a" if q.get("retained_growing") is None else ("yes" if q["retained_growing"] else "no")} |
| Valuation | P/E ratio | {"n/a" if q.get("pe") is None else f"{q['pe']:.1f}"} |

{("**Growth quality:** " + q["growth_quality"]) if q.get("growth_quality") else ""}"""


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
## Quality ({_qscore(m)})
{_qtable(m)}

## Forensic checks
{_ftable(m)}

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
    ap.add_argument("--top", type=int,
                    help="scan the N largest US companies on SEC EDGAR (plus your watchlist) and save "
                         "the ones that pass to output/discovered.txt for the daily signals")
    ap.add_argument("--ai", action="store_true", help="draft catalysts/risks with Claude API")
    ap.add_argument("--all-theses", action="store_true", help="write a thesis for every company, not just passes")
    ap.add_argument("--offline-dir", help=argparse.SUPPRESS)  # for testing with saved JSON
    a = ap.parse_args()

    ed = Edgar(a.offline_dir)
    watch = read_watchlist()
    universe = set()
    if a.tickers:
        tickers = [t.upper() for t in a.tickers]
    elif a.top:
        # SEC's list runs roughly largest company first. One ticker per company (skip extra share classes).
        seen, top = set(), []
        for t, cik, _ in ed.tickers():
            if cik in seen or not re.fullmatch(r"[A-Z]{1,6}", t):
                continue
            seen.add(cik); top.append(t)
            if len(top) >= a.top:
                break
        universe = set(top)
        tickers = list(dict.fromkeys(watch + top))
    else:
        tickers = watch

    print(f"Screening {len(tickers)} companies" + (f" ({len(universe)} largest + your watchlist)" if a.top else "") + "...")
    results, skipped_fin, errors = [], 0, 0
    for i, t in enumerate(tickers, 1):
        cik, name = ed.lookup(t)
        if not cik:
            print(f"  {t}: not found in SEC list"); continue
        sic, sic_desc = ed.industry(cik)
        if is_financial(sic) and t not in watch:
            skipped_fin += 1          # don't even download: valuation doesn't fit
            continue
        try:
            m = analyse(t, name, ed.facts(cik))
        except Exception as e:
            errors += 1
            if t in watch or not a.top:
                print(f"  {t}: data error ({e})")
            continue
        if not m:
            if t in watch or not a.top:
                print(f"  {t}: not enough annual data")
            continue
        m["financial"], m["industry"], m["sic"] = is_financial(sic), sic_desc, sic
        m["source"] = "watchlist" if t in watch else "scan"
        results.append(m)
        if a.top and i % 50 == 0:
            print(f"  ...{i}/{len(tickers)} checked")

    prices = load_prices([m["ticker"] for m in results])
    for m in results:
        m["price"] = prices.get(m["ticker"])
        value(m); screen(m)
        if not a.top or m["passes"] or m["source"] == "watchlist":
            print(f"  {m['ticker']}: {'PASS' if m['passes'] else 'fail'}"
                  f"  price {ps(m['price'])}  base value {ps(m['iv_base'])}")

    OUT.mkdir(exist_ok=True)
    (OUT / "theses").mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    with (OUT / f"screen_{stamp}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "passes", "price", "value_bear", "value_base", "value_bull",
                    "margin_of_safety", "revenue_growth", "revenue_cagr", "net_margin",
                    "fcf_latest", "fcf_avg3", "fcf_improving", "debt_latest", "debt_declining",
                    "share_change_pa", "why_not", "source", "industry",
                    "quality_score", "quality_of", "roic", "cash_conversion", "eps_cagr", "pe", "growth_quality",
                    "beneish", "piotroski", "piotroski_of", "altman", "altman_zone", "accruals"])
        for m in sorted(results, key=lambda x: (not x["passes"], -(x["margin_of_safety"] or -9))):
            w.writerow([m["ticker"], m["name"], m["passes"], m["price"], m["iv_bear"], m["iv_base"],
                        m["iv_bull"], m["margin_of_safety"], m["revenue_growth"], m["revenue_cagr"],
                        m["net_margin"], m["fcf_latest"], m["fcf_avg3"], m["fcf_improving"],
                        m["debt_latest"], m["debt_declining"], m["share_change_pa"],
                        "; ".join(m["fail_reasons"]), m["source"], m["industry"],
                        m["quality"]["score"], m["quality"]["score_of"], m["quality"]["roic"],
                        m["quality"]["cash_conversion"], m["quality"]["eps_cagr"], m["quality"].get("pe"),
                        m["quality"]["growth_quality"],
                        m["forensic"].get("beneish"), m["forensic"].get("piotroski"), m["forensic"].get("piotroski_of"),
                        m["forensic"].get("altman"), m["forensic"].get("altman_zone"), m["forensic"].get("accruals")])

    # theses: every pass, plus your own watchlist names (so you can see why they failed)
    chosen = [m for m in results if a.all_theses or m["passes"] or (a.top and m["source"] == "watchlist")]
    for m in chosen:
        ai = ai_draft(m) if a.ai else None
        (OUT / "theses" / f"{m['ticker']}_{stamp}.md").write_text(thesis_md(m, ai))

    passed = [m for m in results if m["passes"]]
    if a.top:
        found = [m["ticker"] for m in passed if m["source"] == "scan"]
        import playbook
        bears = []
        for m in results:
            if m.get("financial") or m["passes"]:
                continue
            n, signs = playbook.bear_score(m)
            if n >= 2:          # the daily chart check can add the 3rd sign (price breaking down)
                bears.append((m["ticker"], n, signs))
        bears.sort(key=lambda b: -b[1])
        (OUT / "bearish.txt").write_text(
            f"# Bearish setups from the weekly scan on {stamp}: 2+ warning signs in the accounts.\n"
            f"# The daily signals check their charts and price a capped-risk put spread at 3+ signs.\n"
            + "".join(f"{t}  # {n}: {'; '.join(sg)}\n" for t, n, sg in bears[:40]))
        (OUT / "discovered.txt").write_text(
            f"# Found by the weekly scan on {stamp}: passed the fundamental screen.\n"
            f"# Rewritten every week. The daily signals check these as well as watchlist.txt.\n"
            + "".join(t + "\n" for t in found))
        (OUT / "scan_summary.json").write_text(json.dumps({
            "date": stamp, "scanned": len(universe), "analysed": len(results),
            "skipped_financials": skipped_fin, "data_errors": errors,
            "passed": len(passed), "discovered": found, "bearish": [b[0] for b in bears[:40]]}, indent=1))
        print(f"\nScan: {len(universe)} largest companies, {skipped_fin} banks/insurers/REITs skipped, "
              f"{errors} without usable data. {len(found)} new candidates saved to output/discovered.txt")
    print(f"Done: {len(passed)} of {len(results)} passed. "
          f"Results in {OUT/f'screen_{stamp}.csv'}, theses in {OUT/'theses'}")


if __name__ == "__main__":
    main()
