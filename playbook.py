"""
Options playbook: one capped-risk trade idea per stock
-------------------------------------------------------
Only strategies where the most you can lose is known in advance:

  LEAPS call         green ★★ with quality >= 5 : buy a 12-24 month, slightly in-the-money call
  Bull call spread   other greens                : buy a call near the price, sell one near base value
  LEAPS put          bearish ★★                  : buy a 12-24 month, at-the-money put
  Bear put spread    other bearish setups        : buy a put near the price, sell one near the downside target

Bearish stars mirror the green ones: ★ the weekly chart confirms a downtrend (below a falling
30-week average, weekly MACD negative), ★ severe deterioration (4+ warning signs and quality <= 2/6).

Never suggested: naked short calls/puts, short straddles/strangles, ratio spreads or anything else
with uncapped risk.

Pricing is deliberately conservative: you PAY the ask on every leg you buy and RECEIVE the bid on
every leg you sell. An idea is only shown if reward at the target is at least MIN_REWARD_RISK x the
maximum loss, and it is sized so the maximum loss is about RISK_PER_TRADE of the account.

Ideas only - nothing here places a trade. Not investment advice.
"""
import json, math, os, re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

PLAY = {
    "account_size": 10_000,     # used when there's no paper account to read the balance from
    "risk_per_trade": 0.015,    # max loss per idea = 1.5% of the account
    "min_reward_risk": 2.0,     # only show ideas with reward >= 2x risk at the target price
    "leaps_min_days": 365, "leaps_max_days": 760,
    "spread_min_days": 150, "spread_max_days": 400,
    "bear_min_days": 75, "bear_max_days": 200,
    "min_open_interest": 10,    # skip illiquid strikes when the data has it
}

OCC = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


# --------------------------------------------------------------------------------------
# OPTION CHAINS
# --------------------------------------------------------------------------------------
def _alpaca_chain(sym, d_min, d_max, kind):
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        return None
    base = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
    params = {"feed": os.environ.get("ALPACA_OPTIONS_FEED", "indicative"), "limit": 1000, "type": kind,
              "expiration_date_gte": (date.today() + timedelta(days=d_min)).isoformat(),
              "expiration_date_lte": (date.today() + timedelta(days=d_max)).isoformat()}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    out = []
    for _ in range(10):
        r = requests.get(f"{base}/v1beta1/options/snapshots/{sym}", params=params, headers=headers, timeout=30)
        if r.status_code >= 400:
            return None
        j = r.json()
        for occ, snap in (j.get("snapshots") or {}).items():
            mm = OCC.match(occ)
            if not mm:
                continue
            q = snap.get("latestQuote") or {}
            out.append({"symbol": occ, "type": "call" if mm.group(3) == "C" else "put",
                        "expiry": datetime.strptime(mm.group(2), "%y%m%d").date().isoformat(),
                        "strike": int(mm.group(4)) / 1000, "bid": q.get("bp"), "ask": q.get("ap"),
                        "iv": snap.get("impliedVolatility"), "oi": None})
        if not j.get("next_page_token"):
            break
        params["page_token"] = j["next_page_token"]
    return out or None


def _yahoo_chain(sym, d_min, d_max, kind):
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        out = []
        for e in t.options:
            days = (date.fromisoformat(e) - date.today()).days
            if not (d_min <= days <= d_max):
                continue
            ch = t.option_chain(e)
            df = ch.calls if kind == "call" else ch.puts
            for _, row in df.iterrows():
                out.append({"symbol": row.get("contractSymbol"), "type": kind, "expiry": e,
                            "strike": float(row["strike"]),
                            "bid": float(row["bid"]) if row.get("bid") == row.get("bid") else None,
                            "ask": float(row["ask"]) if row.get("ask") == row.get("ask") else None,
                            "iv": float(row["impliedVolatility"]) if row.get("impliedVolatility") == row.get("impliedVolatility") else None,
                            "oi": int(row["openInterest"]) if row.get("openInterest") == row.get("openInterest") else None})
        return out or None
    except Exception:
        return None


def chain(sym, d_min, d_max, kind, offline_dir=None):
    if offline_dir:
        p = Path(offline_dir) / f"options_{sym}.json"
        if not p.exists():
            return None
        rows = json.loads(p.read_text())
        return [r for r in rows if r["type"] == kind and
                d_min <= (date.fromisoformat(r["expiry"]) - date.today()).days <= d_max] or None
    return _alpaca_chain(sym, d_min, d_max, kind) or _yahoo_chain(sym, d_min, d_max, kind)


def _usable(o):
    return (o.get("ask") or 0) > 0 and (o.get("oi") is None or o["oi"] >= PLAY["min_open_interest"])


def _pick_expiry(rows, target_days):
    exps = sorted({r["expiry"] for r in rows})
    return min(exps, key=lambda e: abs((date.fromisoformat(e) - date.today()).days - target_days)) if exps else None


def _nearest(rows, strike):
    rows = [r for r in rows if _usable(r)]
    return min(rows, key=lambda r: abs(r["strike"] - strike)) if rows else None


# --------------------------------------------------------------------------------------
# STRUCTURES
# --------------------------------------------------------------------------------------
def leaps_call(sym, price, target, offline_dir=None):
    rows = chain(sym, PLAY["leaps_min_days"], PLAY["leaps_max_days"], "call", offline_dir)
    if not rows:
        return None
    exp = _pick_expiry(rows, 500)
    leg = _nearest([r for r in rows if r["expiry"] == exp], price * 0.90)   # ~10% in the money
    if not leg:
        return None
    cost = leg["ask"]
    reward = max(target - leg["strike"], 0) - cost
    return {"strategy": "LEAPS call", "bias": "bullish", "expiry": exp,
            "legs": [f"Buy {exp} ${leg['strike']:g} call @ ${cost:.2f}"],
            "max_loss": cost, "max_gain": None, "gain_at_target": reward,
            "breakeven": leg["strike"] + cost, "target": target, "iv": leg.get("iv")}


def bull_call_spread(sym, price, target, offline_dir=None):
    rows = chain(sym, PLAY["spread_min_days"], PLAY["spread_max_days"], "call", offline_dir)
    if not rows or target <= price * 1.05:
        return None
    exp = _pick_expiry(rows, 270)
    rows = [r for r in rows if r["expiry"] == exp]
    long_ = _nearest(rows, price)
    short = _nearest([r for r in rows if r["strike"] > (long_ or {}).get("strike", 1e9)], target)
    if not (long_ and short) or short.get("bid") is None:
        return None
    debit = long_["ask"] - short["bid"]
    width = short["strike"] - long_["strike"]
    if debit <= 0 or width <= 0:
        return None
    gain_at_target = min(max(target - long_["strike"], 0), width) - debit
    return {"strategy": "Bull call spread", "bias": "bullish", "expiry": exp,
            "legs": [f"Buy {exp} ${long_['strike']:g} call @ ${long_['ask']:.2f}",
                     f"Sell {exp} ${short['strike']:g} call @ ${short['bid']:.2f}"],
            "max_loss": debit, "max_gain": width - debit, "gain_at_target": gain_at_target,
            "breakeven": long_["strike"] + debit, "target": target, "iv": long_.get("iv")}


def leaps_put(sym, price, target, offline_dir=None):
    rows = chain(sym, PLAY["leaps_min_days"], PLAY["leaps_max_days"], "put", offline_dir)
    if not rows or target >= price * 0.95:
        return None
    exp = _pick_expiry(rows, 500)
    leg = _nearest([r for r in rows if r["expiry"] == exp], price)   # at the money: less time value to pay
    if not leg:
        return None
    cost = leg["ask"]
    reward = max(leg["strike"] - target, 0) - cost
    return {"strategy": "LEAPS put", "bias": "bearish", "expiry": exp,
            "legs": [f"Buy {exp} ${leg['strike']:g} put @ ${cost:.2f}"],
            "max_loss": cost, "max_gain": leg["strike"] - cost,      # share price can't go below zero
            "gain_at_target": reward, "breakeven": leg["strike"] - cost, "target": target,
            "iv": leg.get("iv")}


def bear_put_spread(sym, price, target, offline_dir=None):
    rows = chain(sym, PLAY["bear_min_days"], PLAY["bear_max_days"], "put", offline_dir)
    if not rows or target >= price * 0.95:
        return None
    exp = _pick_expiry(rows, 120)
    rows = [r for r in rows if r["expiry"] == exp]
    long_ = _nearest(rows, price)
    short = _nearest([r for r in rows if r["strike"] < (long_ or {}).get("strike", -1)], target)
    if not (long_ and short) or short.get("bid") is None:
        return None
    debit = long_["ask"] - short["bid"]
    width = long_["strike"] - short["strike"]
    if debit <= 0 or width <= 0:
        return None
    gain_at_target = min(max(long_["strike"] - target, 0), width) - debit
    return {"strategy": "Bear put spread", "bias": "bearish", "expiry": exp,
            "legs": [f"Buy {exp} ${long_['strike']:g} put @ ${long_['ask']:.2f}",
                     f"Sell {exp} ${short['strike']:g} put @ ${short['bid']:.2f}"],
            "max_loss": debit, "max_gain": width - debit, "gain_at_target": gain_at_target,
            "breakeven": long_["strike"] - debit, "target": target, "iv": long_.get("iv")}


# --------------------------------------------------------------------------------------
# BEARISH SETUP SCORE (from the quality pack)
# --------------------------------------------------------------------------------------
def bear_score(m, breakdown=None):
    """Count independent warning signs. 3+ = bearish setup worth a capped-risk put idea."""
    q = (m or {}).get("quality") or {}
    signs = []
    if q.get("retained") is not None and (q["retained"] < 0 or q.get("retained_growing") is False):
        signs.append("retained earnings negative or shrinking")
    if q.get("payout_of_fcf") is not None and q["payout_of_fcf"] > 1.2:
        signs.append("paying out more than it generates")
    gq = q.get("growth_quality") or ""
    if gq.startswith("EPS rising while revenue falls"):
        signs.append("EPS rising while revenue falls")
    if (q.get("interest_cover") is not None and q["interest_cover"] < 3) or \
       (q.get("current_ratio") is not None and q["current_ratio"] < 1):
        signs.append("stretched finances (interest cover or current ratio)")
    if q.get("cash_conversion") is not None and q["cash_conversion"] < 0.5:
        signs.append("profit not turning into cash")
    if breakdown:
        signs.append("price breaking down")
    return len(signs), signs


# --------------------------------------------------------------------------------------
# DECIDE
# --------------------------------------------------------------------------------------
def suggest(r, account_size=None, offline_dir=None):
    """r = one signals result (needs signal, stars, m, t, ticker). Returns an idea dict or None."""
    m, t = r.get("m") or {}, r["t"]
    price = t["price"]
    q = m.get("quality") or {}
    acct = account_size or PLAY["account_size"]
    idea, why = None, ""
    try:
        if r["signal"] == "GREEN" and m.get("iv_base"):
            if r.get("stars", 0) >= 2 and q.get("score", 0) >= 5:
                idea = leaps_call(r["ticker"], price, m["iv_base"], offline_dir)
                why = "★★ green with quality 5+/6: long-dated call gives time for the value to show"
            if idea is None or (idea["gain_at_target"] < PLAY["min_reward_risk"] * idea["max_loss"]):
                alt = bull_call_spread(r["ticker"], price, m["iv_base"], offline_dir)
                if alt:
                    idea, why = alt, "green: cheap defined-risk spread capped at the base-case value"
        elif r.get("bear_score", 0) >= 3:
            target = price * 0.75
            if m.get("iv_bear") and 0 < m["iv_bear"] < price:
                target = max(m["iv_bear"], price * 0.6)
            if r.get("bear_stars", 0) >= 2:
                # severe cases: deeper target from the bear-case value, kept between -30% and -60%
                deep = price * 0.5 if not (m.get("iv_bear") and m["iv_bear"] > 0) else m["iv_bear"]
                deep = min(max(deep, price * 0.40), price * 0.70)
                idea = leaps_put(r["ticker"], price, deep, offline_dir)
                why = (f"bearish ★★ ({r['bear_score']} warning signs, weekly downtrend, weak quality): "
                       "long-dated put gives the deterioration time to show; loss capped at what you pay")
            if idea is None or (idea["gain_at_target"] < PLAY["min_reward_risk"] * idea["max_loss"]):
                alt = bear_put_spread(r["ticker"], price, target, offline_dir)
                if alt:
                    idea = alt
                    why = f"bearish setup ({r['bear_score']} warning signs): defined-risk put spread, never a short sale"
    except Exception as e:
        return {"error": str(e)[:120]}
    if not idea:
        return None
    rr = idea["gain_at_target"] / idea["max_loss"] if idea["max_loss"] > 0 else 0
    idea["reward_risk"] = rr
    idea["why"] = why
    idea["meets_rule"] = rr >= PLAY["min_reward_risk"]
    budget = acct * PLAY["risk_per_trade"]
    idea["contracts"] = int(budget // (idea["max_loss"] * 100)) if idea["max_loss"] > 0 else 0
    idea["risk_budget"] = budget
    idea["max_loss_total"] = idea["contracts"] * idea["max_loss"] * 100
    return idea


def describe(idea):
    if not idea or idea.get("error"):
        return ""
    mg = "unlimited" if idea["max_gain"] is None else f"${idea['max_gain']*100:,.0f}"
    size = (f"{idea['contracts']} contract(s) = max loss ${idea['max_loss_total']:,.0f}" if idea["contracts"] >= 1
            else f"1 contract would risk ${idea['max_loss']*100:,.0f}, above your ${idea['risk_budget']:,.0f} limit")
    return (f"{idea['strategy']} ({idea['expiry']}): " + "; ".join(idea["legs"]) +
            f". Per contract: max loss ${idea['max_loss']*100:,.0f}, max gain {mg}, "
            f"break-even ${idea['breakeven']:,.2f}, reward at target ${idea['target']:,.2f} = "
            f"{idea['reward_risk']:.1f}x risk. {size}.")
