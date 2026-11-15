"""
Go-live scorecard for the paper account
---------------------------------------
Measures the paper-trading results against the pass marks agreed before starting, so the
decision to go live is made on rules set in advance, not on how the numbers feel.

Reads output/paper_history.csv, paper_journal.csv and paper_positions.json (written by paper.py)
and writes output/scorecard.json. Run on its own with:  python scorecard.py
"""
import csv, json
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "output"

GOALS = {
    "trial_days": 90,              # decision after ~3 months (extend to 6 if too few trades)
    "beat_spx_by": 0.02,           # beat the S&P 500 by at least 2 percentage points
    "min_trades": 15,              # at least 15 positions taken
    "max_drawdown": 0.15,          # never more than 15% below the account's peak
}


def _read_csv(p):
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def trades():
    """Pair BUYs and SELLs per ticker (first in, first out). Open positions use today's price."""
    journal = _read_csv(OUT / "paper_journal.csv")
    positions = {p["ticker"]: p for p in json.loads((OUT / "paper_positions.json").read_text())} \
        if (OUT / "paper_positions.json").exists() else {}
    lots, done = {}, []
    for row in journal:
        tk, qty, px = row["ticker"], float(row["qty"]), float(row["approx_price"])
        if row["action"] == "BUY":
            lots.setdefault(tk, []).append({"qty": qty, "price": px, "date": row["date"]})
        elif row["action"] == "SELL":
            remaining = qty
            while remaining > 0 and lots.get(tk):
                lot = lots[tk][0]
                q = min(lot["qty"], remaining)
                done.append({"ticker": tk, "opened": lot["date"], "closed": row["date"], "qty": q,
                             "entry": lot["price"], "exit": px, "pnl": (px - lot["price"]) * q,
                             "pnl_pct": px / lot["price"] - 1, "open": False, "reason": row.get("reason", "")})
                lot["qty"] -= q; remaining -= q
                if lot["qty"] <= 1e-9:
                    lots[tk].pop(0)
    for tk, ls in lots.items():
        for lot in ls:
            if lot["qty"] <= 0:
                continue
            cur = (positions.get(tk) or {}).get("price") or lot["price"]
            done.append({"ticker": tk, "opened": lot["date"], "closed": None, "qty": lot["qty"],
                         "entry": lot["price"], "exit": cur, "pnl": (cur - lot["price"]) * lot["qty"],
                         "pnl_pct": cur / lot["price"] - 1, "open": True, "reason": ""})
    return done


def build():
    hist = [r for r in _read_csv(OUT / "paper_history.csv") if r.get("equity")]
    if not hist:
        return None
    eq = [float(r["equity"]) for r in hist]
    start, last = hist[0], hist[-1]
    my_ret = eq[-1] / eq[0] - 1
    spx_ret = (float(last["spy"]) / float(start["spy"]) - 1) if start.get("spy") and last.get("spy") else None
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    tr = trades()
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    best = max(tr, key=lambda t: t["pnl"]) if tr else None
    worst = min(tr, key=lambda t: t["pnl"]) if tr else None
    ex_best = ((eq[-1] - eq[0] - (best["pnl"] if best and best["pnl"] > 0 else 0)) / eq[0]) if eq else None
    started = date.fromisoformat(start["date"])
    decision = started + timedelta(days=GOALS["trial_days"])

    tests = [
        {"test": f"Beats the S&P 500 by {GOALS['beat_spx_by']*100:.0f}+ points",
         "value": None if spx_ret is None else my_ret - spx_ret,
         "pass": None if spx_ret is None else (my_ret - spx_ret) >= GOALS["beat_spx_by"]},
        {"test": f"At least {GOALS['min_trades']} positions taken", "value": len(tr),
         "pass": len(tr) >= GOALS["min_trades"]},
        {"test": f"Worst drop from peak under {GOALS['max_drawdown']*100:.0f}%", "value": mdd,
         "pass": mdd <= GOALS["max_drawdown"]},
        {"test": "Still beats the S&P 500 without the single best trade",
         "value": None if (ex_best is None or spx_ret is None) else ex_best - spx_ret,
         "pass": None if (ex_best is None or spx_ret is None) else ex_best > spx_ret},
    ]
    passed = sum(1 for t in tests if t["pass"])
    days = (date.today() - started).days
    if days < GOALS["trial_days"]:
        verdict = f"Trial running: day {days} of {GOALS['trial_days']} (decision on {decision.isoformat()})."
    elif len(tr) < GOALS["min_trades"]:
        verdict = "Too few trades to judge: extend the trial to 6 months."
    elif passed == len(tests):
        verdict = "All tests passed: consider going live small, placing trades yourself."
    else:
        verdict = f"{passed} of {len(tests)} tests passed: keep paper trading and review the rules."

    card = {
        "updated": date.today().isoformat(), "started": start["date"], "decision_date": decision.isoformat(),
        "days": days, "start_equity": eq[0], "equity": eq[-1], "return": my_ret, "spx_return": spx_ret,
        "max_drawdown": mdd, "trades": len(tr), "open": sum(t["open"] for t in tr),
        "closed": sum(not t["open"] for t in tr),
        "win_rate": (len(wins) / len(tr)) if tr else None,
        "avg_win": (sum(t["pnl_pct"] for t in wins) / len(wins)) if wins else None,
        "avg_loss": (sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else None,
        "best": best, "worst": worst, "return_ex_best": ex_best,
        "tests": tests, "passed": passed, "verdict": verdict,
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "scorecard.json").write_text(json.dumps(card, indent=1, default=str))
    return card


def summary_lines(card):
    if not card:
        return []
    p = lambda x: "n/a" if x is None else f"{x*100:+.1f}%"
    wr = "n/a" if card["win_rate"] is None else f"{card['win_rate']*100:.0f}%"
    lines = [f"Scorecard (day {card['days']}): account {p(card['return'])} vs S&P 500 {p(card['spx_return'])}, "
             f"worst drop {card['max_drawdown']*100:.1f}%, {card['trades']} positions, win rate {wr}"]
    lines.append(card["verdict"])
    return lines


if __name__ == "__main__":
    c = build()
    print("\n".join(summary_lines(c)) if c else "No paper history yet.")
