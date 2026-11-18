"""
Paper trading on an Alpaca demo account (fake $100k, real prices)
----------------------------------------------------------------
Used by:  python signals.py --paper              (weekly)
          python signals.py --mode daily --paper (daily)

What it does on each run:
  * your paper POSITIONS are treated as holdings, so the sell rules apply to them
    (target value reached, thesis broken, price breakdown, stop-loss)
  * BUY every GREEN stock you don't already hold (bigger position for each ★)
  * SELL every held stock that turns RED
  * logs every order to output/paper_journal.csv and tracks performance vs the S&P 500

Shares only for now. Orders are market orders placed for the next US open.
No real money is involved: it only ever talks to Alpaca's PAPER endpoint.
"""
import csv, json, math, os
from datetime import date
from pathlib import Path

import requests

HERE = Path(__file__).parent
OUT = HERE / "output"

PAPER = {
    # --- risk limits (change these numbers to loosen/tighten) ---
    "size_green": 0.02,      # 2% of account per green, +1.5% per star:
    "size_star": 0.015,      #   ★ = weekly chart agrees, ★ = smart money buying
    "size_max": 0.05,        # never more than 5% of the account in one stock
    "max_positions": 10,
    "max_invested": 0.50,    # never more than 50% of the account in stocks; the rest stays cash
    "max_new_per_run": 3,    # build positions gradually, not all on one day
    "stop_loss": 0.15,       # paper positions exit 15% below entry
}

PAPER_URL = "https://paper-api.alpaca.markets"


class Alpaca:
    def __init__(self):
        key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
        if not (key and secret):
            raise RuntimeError("Set ALPACA_KEY_ID and ALPACA_SECRET_KEY in settings.txt (see README).")
        self.base = os.environ.get("ALPACA_BASE_URL", PAPER_URL).rstrip("/")
        if "paper" not in self.base and not self.base.startswith("http://127.0.0.1"):
            raise RuntimeError("Safety stop: ALPACA_BASE_URL must be the PAPER endpoint.")
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path}: {r.status_code} {r.text[:200]}")
        return r.json() if r.text else {}

    def account(self):
        return self._req("GET", "/v2/account")

    def positions(self):
        return {p["symbol"]: p for p in self._req("GET", "/v2/positions")}

    def open_orders(self):
        return {o["symbol"]: o for o in self._req("GET", "/v2/orders", params={"status": "open"})}

    def buy(self, symbol, qty):
        return self._req("POST", "/v2/orders", json={
            "symbol": symbol, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day"})

    def close(self, symbol):
        return self._req("DELETE", f"/v2/positions/{symbol}")


def paper_holdings(api):
    """Paper positions in the same shape as holdings.csv rows."""
    out = {}
    for sym, p in api.positions().items():
        entry = float(p["avg_entry_price"])
        out[sym] = {"entry": entry, "stop": entry * (1 - PAPER["stop_loss"]), "qty": float(p["qty"])}
    return out


def journal(rows):
    OUT.mkdir(exist_ok=True)
    p = OUT / "paper_journal.csv"
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "ticker", "action", "qty", "approx_price", "signal", "reason"])
        w.writerows(rows)


def execute(api, results):
    """Place paper orders from the signals. Returns a list of human-readable action lines."""
    acct = api.account()
    equity = float(acct["equity"])
    cash = float(acct["cash"])
    held = api.positions()
    pending = api.open_orders()
    actions, rows, today = [], [], date.today().isoformat()

    # Sells first (frees cash)
    for r in results:
        if r["ticker"] in held and r["signal"] == "RED" and r["ticker"] not in pending:
            try:
                api.close(r["ticker"])
            except RuntimeError as e:
                actions.append(f"FAILED to sell {r['ticker']}: {str(e)[:120]}")
                continue
            q = float(held[r["ticker"]]["qty"])
            qty = int(q) if q == int(q) else q
            reason = r["why"][0].replace("<b>", "").replace("</b>", "")
            actions.append(f"SELL {qty} {r['ticker']} ({reason})")
            rows.append([today, r["ticker"], "SELL", qty, r["t"]["price"], r["signal"], reason])

    # Buys: strong greens first, then normal greens
    n_pos = len([s for s in held if s not in {x["ticker"] for x in results if x["signal"] == "RED"}])
    greens = sorted([r for r in results if r["signal"] == "GREEN" and not r["holding"]
                     and r["ticker"] not in held and r["ticker"] not in pending],
                    key=lambda r: (-r.get("stars", 0), -r["t"]["score"]))
    invested = max(equity - cash, 0.0)
    n_new = 0
    for r in greens:
        if n_pos >= PAPER["max_positions"]:
            actions.append(f"SKIPPED {r['ticker']}: already at {PAPER['max_positions']} positions")
            continue
        if n_new >= PAPER["max_new_per_run"]:
            actions.append(f"SKIPPED {r['ticker']}: max {PAPER['max_new_per_run']} new buys per run")
            continue
        room = equity * PAPER["max_invested"] - invested
        size = min(PAPER["size_green"] + PAPER["size_star"] * r.get("stars", 0), PAPER["size_max"])
        amount = min(equity * size, cash * 0.98, room)
        if amount <= 0:
            actions.append(f"SKIPPED {r['ticker']}: already {PAPER['max_invested']:.0%} invested (cash limit)")
            continue
        qty = math.floor(amount / r["t"]["price"])
        if qty < 1:
            actions.append(f"SKIPPED {r['ticker']}: not enough paper cash")
            continue
        try:
            api.buy(r["ticker"], qty)
        except RuntimeError as e:
            actions.append(f"FAILED to buy {r['ticker']}: {str(e)[:120]}")
            continue
        cash -= qty * r["t"]["price"]
        invested += qty * r["t"]["price"]
        n_pos += 1
        n_new += 1
        kind = "green " + "★" * r.get("stars", 0) if r.get("stars") else "green"
        actions.append(f"BUY {qty} {r['ticker']} at ~${r['t']['price']:,.2f} ({kind}, ~${qty*r['t']['price']:,.0f})")
        rows.append([today, r["ticker"], "BUY", qty, r["t"]["price"], "GREEN " + "★" * r.get("stars", 0),
                     "; ".join(x.replace("<b>", "").replace("</b>", "") for x in r["why"])])

    if rows:
        journal(rows)
    snapshot = [{"ticker": k, "qty": float(p["qty"]), "entry": float(p["avg_entry_price"]),
                 "price": float(p.get("current_price") or 0) or None,
                 "pnl_pct": float(p["unrealized_plpc"]) if p.get("unrealized_plpc") else None}
                for k, p in api.positions().items()]
    OUT.mkdir(exist_ok=True)
    (OUT / "paper_positions.json").write_text(json.dumps(snapshot, indent=1))
    return actions


def performance(api, spy_price):
    """Paper account return since the first run vs SPY over the same period."""
    acct = api.account()
    equity = float(acct["equity"])
    p = OUT / "paper_start.json"
    OUT.mkdir(exist_ok=True)
    if not p.exists():
        p.write_text(json.dumps({"date": date.today().isoformat(), "equity": equity, "spy": spy_price}))
    start = json.loads(p.read_text())
    mine = equity / start["equity"] - 1
    spy = (spy_price / start["spy"] - 1) if start.get("spy") and spy_price else None
    # daily history for the dashboard chart (one row per date)
    hp = OUT / "paper_history.csv"
    rows = {}
    if hp.exists():
        with hp.open() as f:
            for row in csv.DictReader(f):
                rows[row["date"]] = row
    rows[date.today().isoformat()] = {"date": date.today().isoformat(), "equity": f"{equity:.2f}",
                                      "spy": f"{spy_price:.4f}" if spy_price else ""}
    with hp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "equity", "spy"])
        w.writeheader(); w.writerows(rows[k] for k in sorted(rows))
    line = f"Paper account ${equity:,.0f} ({mine*100:+.1f}% since {start['date']}"
    line += f", S&P 500 {spy*100:+.1f}%)" if spy is not None else ")"
    return line
