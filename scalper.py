"""
Scalping replay: tests the three setups from the scalping framework doc on SPY 1-minute bars
-------------------------------------------------------------------------------------------
GitHub Actions can't watch the market minute by minute, so this replays each finished day
instead: it walks through the day's 1-minute bars in order and applies the rules exactly as
written, only ever using bars that had already closed at that moment.

  Setup A  opening range breakout   first 15 minutes' high/low, 5-min trend filter, stop at the
                                    middle of the range, target 1.5R
  Setup B  trend pullback           5-min 20 EMA above 50 EMA (both rising), pullback to the
                                    1-min 20 EMA, entry on a close above the previous high
  Setup C1 failed breakout          spike past yesterday's high/low, 5-min close back inside,
                                    stop beyond the spike, target yesterday's close or 1.5R
  Setup C2 breakout and retest      close past yesterday's high/low, pull back to it, hold,
                                    target 1.5R
  Setup D1 80% rule (auction)       opens outside yesterday's value area, then two 30-min
                                    periods close back inside: trade to the other side of value
  Setup D2 value edge rejection     opens inside value, pokes past the value area high/low, a
                                    5-min close back inside: trade back to the point of control

  Value area = the price band holding 70% of yesterday's volume (volume spread evenly across
  each 1-minute bar's range, in 5-cent steps); point of control (POC) = the busiest price.

Window 09:30-11:00 New York time (14:30-16:00 UK) for A-C. The auction setups (D) need
30-minute periods, so they run until 12:30 New York (17:30 UK); A-C trades still close at 11:00. Risk 0.5% of the account per trade on a
GBP 5,000 demo account; stop for the day at -1.5%, after 3 losses in a row or 6 trades; stop
for the week at -4%; one trade at a time; anything open is closed at 11:00.

Fills: entry at the next bar's open; if a bar touches both stop and target, the stop is assumed
to come first (the cautious choice). Costs: a round-trip charge similar to a US500 spread bet.
No news filter yet (no free, reliable calendar), so results include news days.

Each run replays everything from scratch, so results never drift:
  backtest  the ~6 months before the trial started (evidence on day one)
  trial     from the trial start date onwards (the 30-day scorecard)

Outputs: output/scalp_trades.csv, output/scalp_history.csv, output/scalp_summary.json
Offline test:  python scalper.py --offline-csv bars.csv   (columns t,o,h,l,c,v; t in UTC)
"""
import argparse
import csv
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import pandas as pd
import requests

HERE = Path(__file__).parent
OUT = HERE / "output"
NY = ZoneInfo("America/New_York")

RULES = {
    "symbol": "SPY",
    "account": 5000.0,          # demo account, GBP
    "risk": 0.005,              # 0.5% per trade
    "day_loss": 0.015,          # stop for the day
    "week_loss": 0.04,          # stop for the week
    "max_trades": 6,
    "max_losses_in_row": 3,
    "target_r": 1.5,
    "cost": 0.00015,            # round trip, as a share of price (about 1 point on US500)
    "window": (time(9, 30), time(12, 30)),
    "abc_end": time(11, 0),     # A-C: no entries after this and anything open is closed
    "d_last_entry": time(12, 15),
    "va_share": 0.70,           # value area = 70% of yesterday's volume
    "va_step": 0.05,            # price step for the volume profile
    "orb_end": time(9, 45),     # opening range = first 15 minutes
    "orb_last_entry": time(10, 30),
    "backtest_days": 182,
    "trial_days": 30,
}


# ---------------------------------------------------------------- data
def fetch_bars(start, end):
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        raise SystemExit("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
    base = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    for feed in ("sip", "iex"):     # free plans get full-market (sip) history older than 15 minutes
        params = {"timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(),
                  "limit": 10000, "adjustment": "raw", "feed": feed}
        bars, ok = [], True
        while True:
            r = requests.get(f"{base}/v2/stocks/{RULES['symbol']}/bars", params=params, headers=headers, timeout=60)
            if r.status_code >= 400:
                ok = False
                break
            j = r.json()
            bars += j.get("bars") or []
            if not j.get("next_page_token"):
                break
            params["page_token"] = j["next_page_token"]
        if ok and bars:
            print(f"{len(bars)} one-minute bars from Alpaca ({feed} feed)")
            return pd.DataFrame(bars), feed
    raise SystemExit("Could not download 1-minute bars from Alpaca")


def prepare(df):
    """Regular-hours 1-min bars indexed by New York time, plus EMAs on 1-min and 5-min bars."""
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    idx = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
    df = df.set_index(idx)[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
    df = df[~df.index.duplicated()]
    df = df[(df.index.time >= time(9, 30)) & (df.index.time < time(16, 0))]
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    five = df[["open", "high", "low", "close"]].resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    five["ema20"] = five["close"].ewm(span=20, adjust=False).mean()
    five["ema50"] = five["close"].ewm(span=50, adjust=False).mean()
    five["done"] = five.index + pd.Timedelta(minutes=5)       # when the 5-min bar is complete
    return df, five


# ---------------------------------------------------------------- the replay
def value_area(bars):
    """Yesterday's volume profile: (value area low, value area high, point of control)."""
    step = RULES["va_step"]
    prof = {}
    for lo, hi, v in zip(bars["low"], bars["high"], bars["volume"]):
        a, z = int(round(lo / step)), int(round(hi / step))
        share = v / (z - a + 1)
        for k in range(a, z + 1):
            prof[k] = prof.get(k, 0.0) + share
    if not prof:
        return None
    keys = sorted(prof)
    poc = max(keys, key=lambda k: prof[k])
    total, have = sum(prof.values()), prof[poc]
    lo_i = hi_i = keys.index(poc)
    while have < RULES["va_share"] * total and (lo_i > 0 or hi_i < len(keys) - 1):
        up = prof[keys[hi_i + 1]] if hi_i < len(keys) - 1 else -1
        dn = prof[keys[lo_i - 1]] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1
            have += up
        else:
            lo_i -= 1
            have += dn
    return keys[lo_i] * step, keys[hi_i] * step, poc * step


class Day:
    """Everything the rules need to know while walking through one day, bar by bar."""
    def __init__(self, bars, five, prev, ranges, va=None):
        self.b = bars
        self.f = five
        self.prev = prev                      # yesterday's high, low, close
        self.va = va                          # yesterday's value area low, high, point of control
        self.open0 = bars["open"].iloc[0]
        self.inside_run = []                  # D1: consecutive 30-min periods closing inside value
        self.dspike = {"long": None, "short": None}           # D2 poke past the value edge
        o = bars[bars.index.time < RULES["orb_end"]]
        self.rh, self.rl = (o["high"].max(), o["low"].min()) if len(o) else (None, None)
        width = (self.rh - self.rl) if self.rh is not None else None
        self.orb_ok = width is not None and (len(ranges) < 5 or width <= 2 * median(ranges[-20:]))
        self.width = width
        self.used = set()                     # setup+direction already taken today
        self.touch = {"long": None, "short": None}            # Setup B pullback touch
        self.c2 = {"long": None, "short": None}               # Setup C2 state
        self.spike = {"long": None, "short": None}            # Setup C1 spike start

    def five_at(self, ts):
        done = self.f[self.f["done"] <= ts + pd.Timedelta(minutes=1)]
        return done.iloc[-1] if len(done) else None

    def signal(self, i):
        """Look at bar i once it has closed. Returns (setup, side, stop, target_rule) or None."""
        b = self.b
        bar, ts = b.iloc[i], b.index[i]
        prior = b.iloc[i - 1] if i > 0 else None
        f5 = self.five_at(ts)
        hits = []

        # ---- C1: failed breakout of yesterday's high (short) or low (long)
        if self.prev:
            yh, yl, yc = self.prev
            if bar["high"] > yh and self.spike["short"] is None:
                self.spike["short"] = i
            if bar["low"] < yl and self.spike["long"] is None:
                self.spike["long"] = i
            just_done = self.f[self.f["done"] == ts + pd.Timedelta(minutes=1)]
            if len(just_done):
                c5 = just_done.iloc[-1]["close"]
                s = self.spike["short"]
                if s is not None and i - s <= 15 and c5 < yh and "C1short" not in self.used:
                    hits.append(("C1", "short", b["high"].iloc[s:i + 1].max() + 0.01, ("close_or_r", yc)))
                s = self.spike["long"]
                if s is not None and i - s <= 15 and c5 > yl and "C1long" not in self.used:
                    hits.append(("C1", "long", b["low"].iloc[s:i + 1].min() - 0.01, ("close_or_r", yc)))
                # an old spike that never failed stops counting
                for side in ("short", "long"):
                    if self.spike[side] is not None and i - self.spike[side] > 15:
                        self.spike[side] = None

        # ---- A: opening range breakout, first 60 minutes only
        if self.orb_ok and RULES["orb_end"] <= ts.time() and (ts + pd.Timedelta(minutes=1)).time() <= RULES["orb_last_entry"] \
                and prior is not None and f5 is not None:
            mid = (self.rh + self.rl) / 2
            if bar["close"] > self.rh >= prior["close"] and f5["close"] > f5["ema20"] and "Along" not in self.used:
                hits.append(("A", "long", mid, ("r",)))
            if bar["close"] < self.rl <= prior["close"] and f5["close"] < f5["ema20"] and "Ashort" not in self.used:
                hits.append(("A", "short", mid, ("r",)))
            # a breakout that came back inside is finished for the day
            if "Along" not in self.used and prior["close"] > self.rh >= bar["close"]:
                self.used.add("Along")
            if "Ashort" not in self.used and prior["close"] < self.rl <= bar["close"]:
                self.used.add("Ashort")

        # ---- C2: breakout and retest of yesterday's high (long) or low (short)
        if self.prev and prior is not None:
            yh, yl, _ = self.prev
            for side, lvl in (("long", yh), ("short", yl)):
                st, sgn = self.c2[side], (1 if side == "long" else -1)
                if f"C2{side}" in self.used:
                    continue
                if st is None:
                    if sgn * (bar["close"] - lvl) > 0 >= sgn * (prior["close"] - lvl):
                        self.c2[side] = {"broke": i, "retest": None}
                    continue
                if st["retest"] is None:
                    near = (bar["low"] <= lvl * 1.0005) if side == "long" else (bar["high"] >= lvl * 0.9995)
                    if near and sgn * (bar["close"] - lvl) > 0:
                        st["retest"] = i
                    elif sgn * (bar["close"] - lvl) < 0:
                        self.c2[side] = None            # fell back through: wait for a new break
                    continue
                if i - st["retest"] > 5:
                    self.c2[side] = None
                    continue
                trig = bar["close"] > prior["high"] if side == "long" else bar["close"] < prior["low"]
                if trig:
                    seg = b.iloc[st["retest"]:i + 1]
                    stop = seg["low"].min() - 0.01 if side == "long" else seg["high"].max() + 0.01
                    risk = abs(bar["close"] - stop)
                    run = b.iloc[st["broke"]:st["retest"] + 1]
                    ran = (run["high"].max() - lvl) if side == "long" else (lvl - run["low"].min())
                    if risk > 0 and ran <= RULES["target_r"] * risk:
                        hits.append(("C2", side, stop, ("r",)))
                    else:
                        self.used.add(f"C2{side}")

        # ---- B: trend pullback to the 1-min 20 EMA
        if f5 is not None and prior is not None and i >= 30:
            f5s = self.f[self.f["done"] <= ts + pd.Timedelta(minutes=1)]
            if len(f5s) >= 4:
                now, then = f5s.iloc[-1], f5s.iloc[-4]
                up = now["ema20"] > now["ema50"] and now["ema20"] > then["ema20"] and now["ema50"] > then["ema50"]
                dn = now["ema20"] < now["ema50"] and now["ema20"] < then["ema20"] and now["ema50"] < then["ema50"]
                for side, trend in (("long", up), ("short", dn)):
                    if not trend:
                        self.touch[side] = None
                        continue
                    touched = bar["low"] <= bar["ema20"] if side == "long" else bar["high"] >= bar["ema20"]
                    t0 = self.touch[side]
                    if t0 is not None and 0 < i - t0 <= 5:
                        trig = (bar["close"] > prior["high"] and bar["close"] > bar["ema20"]) if side == "long" \
                            else (bar["close"] < prior["low"] and bar["close"] < bar["ema20"])
                        if trig and sum(1 for u in self.used if u.startswith(f"B{side}")) < 2:
                            seg = b.iloc[t0:i + 1]
                            stop = seg["low"].min() - 0.01 if side == "long" else seg["high"].max() + 0.01
                            look = b.iloc[max(0, t0 - 30):t0]
                            swing = (look["high"].max() if side == "long" else look["low"].min()) if len(look) else None
                            hits.append(("B", side, stop, ("swing_or_r", swing)))
                            self.touch[side] = None
                            continue
                    if t0 is not None and i - t0 > 5:
                        self.touch[side] = None
                    if touched and self.touch[side] is None:
                        self.touch[side] = i

        # A-C only trade the 09:30-11:00 window
        if (ts + pd.Timedelta(minutes=1)).time() > RULES["abc_end"]:
            hits = []
        hits += self.auction(i, bar, ts)
        order = {"C1": 0, "D1": 1, "D2": 2, "A": 3, "C2": 4, "B": 5}
        return sorted(hits, key=lambda h: order[h[0]])[0] if hits else None


    def auction(self, i, bar, ts):
        """Setups D1 and D2, from yesterday's value area."""
        if not self.va:
            return []
        val, vah, poc = self.va
        b, hits = self.b, []
        done_at = ts + pd.Timedelta(minutes=1)
        late = done_at.time() > RULES["d_last_entry"]
        mins = (done_at - done_at.replace(hour=9, minute=30)).seconds // 60

        # D1, the 80% rule: opened outside value, two 30-min periods close back inside
        if (self.open0 > vah or self.open0 < val) and mins % 30 == 0 and mins > 0:
            period = b.iloc[max(0, i - 29):i + 1]
            if val <= bar["close"] <= vah:
                self.inside_run.append(period)
            else:
                self.inside_run = []
            if len(self.inside_run) >= 2 and not late and "D1" not in "".join(self.used):
                two = pd.concat(self.inside_run[-2:])
                if self.open0 > vah:
                    hits.append(("D1", "short", two["high"].max() + 0.01, ("level", val)))
                else:
                    hits.append(("D1", "long", two["low"].min() - 0.01, ("level", vah)))

        # D2: opened inside value, poked past an edge, 5-min close back inside: back to the POC
        if val <= self.open0 <= vah:
            if bar["high"] > vah and self.dspike["short"] is None:
                self.dspike["short"] = i
            if bar["low"] < val and self.dspike["long"] is None:
                self.dspike["long"] = i
            if mins % 5 == 0 and not late:
                s = self.dspike["short"]
                if s is not None and i - s <= 15 and bar["close"] < vah and "D2short" not in self.used:
                    hits.append(("D2", "short", b["high"].iloc[s:i + 1].max() + 0.01, ("level", poc)))
                s = self.dspike["long"]
                if s is not None and i - s <= 15 and bar["close"] > val and "D2long" not in self.used:
                    hits.append(("D2", "long", b["low"].iloc[s:i + 1].min() - 0.01, ("level", poc)))
            for side in ("short", "long"):
                if self.dspike[side] is not None and i - self.dspike[side] > 15:
                    self.dspike[side] = None
        return hits


def plan_trade(setup, side, entry, stop, rule):
    sgn = 1 if side == "long" else -1
    risk = sgn * (entry - stop)
    if risk <= 0 or risk < 3 * RULES["cost"] * entry:          # stop too tight: costs would dominate
        return None
    target = entry + sgn * RULES["target_r"] * risk
    if rule[0] == "level":                                      # auction setups aim at a fixed level
        if sgn * (rule[1] - entry) < risk:                      # less than 1R away: not worth it
            return None
        return {"setup": setup, "side": side, "entry": entry, "stop": stop, "target": rule[1], "risk": risk}
    if rule[0] == "close_or_r" and rule[1] is not None and sgn * (rule[1] - entry) > RULES["target_r"] * risk:
        target = rule[1]                                        # yesterday's close is further: use it
    if rule[0] == "swing_or_r" and rule[1] is not None and 0 < sgn * (rule[1] - entry) < RULES["target_r"] * risk:
        if sgn * (rule[1] - entry) < risk:                      # swing too close to be worth it
            return None
        target = rule[1]
    return {"setup": setup, "side": side, "entry": entry, "stop": stop, "target": target, "risk": risk}


def replay(bars, five, days, start_equity):
    """Walk through the given trading days. Returns trades and end-of-day equity."""
    trades, history = [], []
    equity = start_equity
    ranges, week, week_start_eq = [], None, equity
    all_days = sorted(set(bars.index.date))
    for d in days:
        k = all_days.index(d)
        prev_bars = bars[bars.index.date == all_days[k - 1]] if k > 0 else None
        prev = (prev_bars["high"].max(), prev_bars["low"].min(), prev_bars["close"].iloc[-1]) \
            if prev_bars is not None and len(prev_bars) > 300 else None
        b = bars[(bars.index.date == d)]
        b = b[(b.index.time >= RULES["window"][0]) & (b.index.time < RULES["window"][1])]
        f = five[(five.index.date == d)]
        if len(b) < 60:
            continue
        wk = d.isocalendar()[:2]
        if wk != week:
            week, week_start_eq = wk, equity
        va = value_area(prev_bars) if prev is not None else None
        day = Day(b, f, prev, ranges, va)
        if day.width:
            ranges.append(day.width)
        day_start_eq, n, losses_row, pos = equity, 0, 0, None
        stopped = equity <= week_start_eq * (1 - RULES["week_loss"])
        for i in range(len(b) - 1):
            bar = b.iloc[i]
            # A-C trades close at 11:00 New York; the auction setups can run to 12:30
            if pos is not None and i >= pos["i"] and not pos["setup"].startswith("D") \
                    and b.index[i].time() >= RULES["abc_end"]:
                equity, losses_row = close(pos, bar["open"], "window end", b.index[i], equity, trades, losses_row)
                pos = None
                n += 1
            # manage the open trade on this bar
            if pos is not None and i >= pos["i"]:
                sgn = 1 if pos["side"] == "long" else -1
                hit_stop = (bar["low"] <= pos["stop"]) if sgn > 0 else (bar["high"] >= pos["stop"])
                hit_tgt = (bar["high"] >= pos["target"]) if sgn > 0 else (bar["low"] <= pos["target"])
                exit_px = reason = None
                if hit_stop:
                    exit_px, reason = pos["stop"], "stop"
                elif hit_tgt:
                    exit_px, reason = pos["target"], "target"
                if exit_px is not None:
                    equity, losses_row = close(pos, exit_px, reason, b.index[i], equity, trades, losses_row)
                    pos = None
                    n += 1
            sig = day.signal(i)                      # keeps the setups' state up to date every bar
            if pos is not None or stopped or sig is None:
                continue
            if n >= RULES["max_trades"] or losses_row >= RULES["max_losses_in_row"] \
                    or equity <= day_start_eq * (1 - RULES["day_loss"]) \
                    or equity <= week_start_eq * (1 - RULES["week_loss"]):
                stopped = True
                continue
            setup, side, stop, rule = sig
            entry = b["open"].iloc[i + 1]
            t = plan_trade(setup, side, entry, stop, rule)
            day.used.add(f"{setup}{side}" if setup != "B" else f"B{side}{i}")
            if t is None:
                continue
            t.update({"i": i + 1, "time": b.index[i + 1], "size": equity * RULES["risk"] / t["risk"],
                      "risk_gbp": equity * RULES["risk"]})
            pos = t
        if pos is not None:                          # end of window: close at the last close
            equity, losses_row = close(pos, b["close"].iloc[-1], "window end", b.index[-1], equity, trades, losses_row)
        history.append({"date": d.isoformat(), "equity": round(equity, 2)})
    return trades, history


def close(pos, px, reason, ts, equity, trades, losses_row):
    sgn = 1 if pos["side"] == "long" else -1
    gross_r = sgn * (px - pos["entry"]) / pos["risk"]
    cost_r = RULES["cost"] * pos["entry"] / pos["risk"]
    r = gross_r - cost_r
    pnl = r * pos["risk_gbp"]
    equity += pnl
    trades.append({"date": pos["time"].date().isoformat(), "time_ny": pos["time"].strftime("%H:%M"),
                   "time_uk": pos["time"].astimezone(ZoneInfo("Europe/London")).strftime("%H:%M"),
                   "setup": pos["setup"], "side": pos["side"], "entry": round(pos["entry"], 2),
                   "stop": round(pos["stop"], 2), "target": round(pos["target"], 2), "exit": round(px, 2),
                   "exit_time_ny": ts.strftime("%H:%M"), "reason": reason, "r": round(r, 2),
                   "pnl": round(pnl, 2), "equity": round(equity, 2)})
    return equity, (losses_row + 1 if r < 0 else 0)


# ---------------------------------------------------------------- results
def stats(trades, history, start_eq):
    out = {"trades": len(trades)}
    if not trades:
        return out
    rs = [t["r"] for t in trades]
    eq = [start_eq] + [h["equity"] for h in history]
    peak, dd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    out.update({"win_rate": round(sum(r > 0 for r in rs) / len(rs), 3), "avg_r": round(sum(rs) / len(rs), 3),
                "total_r": round(sum(rs), 2), "return": round(eq[-1] / start_eq - 1, 4),
                "end_equity": round(eq[-1], 2), "max_drawdown": round(dd, 4)})
    by = {}
    for s in ("A", "B", "C1", "C2", "D1", "D2"):
        x = [t["r"] for t in trades if t["setup"] == s]
        by[s] = {"trades": len(x), "win_rate": round(sum(r > 0 for r in x) / len(x), 3) if x else None,
                 "avg_r": round(sum(x) / len(x), 3) if x else None, "total_r": round(sum(x), 2)}
    out["by_setup"] = by
    return out


def weekly_results(history, start_eq):
    weeks, prev = {}, start_eq
    for h in history:
        wk = "%d-W%02d" % date.fromisoformat(h["date"]).isocalendar()[:2]
        weeks.setdefault(wk, [prev, None])
        weeks[wk][1] = h["equity"]
        prev = h["equity"]
    return [{"week": k, "pnl": round(v[1] - v[0], 2)} for k, v in weeks.items()]


def scorecard(trial, history, start_eq, start, end):
    weeks = weekly_results(history, start_eq)
    last4 = weeks[-4:]
    tests = [
        {"test": "Number of trades (40 or more)", "value": trial.get("trades", 0), "pass": trial.get("trades", 0) >= 40},
        {"test": "Average result per trade after costs (+0.2R or better)", "value": trial.get("avg_r"),
         "pass": None if trial.get("avg_r") is None else trial["avg_r"] >= 0.2},
        {"test": "Rules followed (90% or more)", "value": "automatic", "pass": True},
        {"test": "Largest drop from a high point (6% or less)", "value": trial.get("max_drawdown"),
         "pass": None if trial.get("max_drawdown") is None else trial["max_drawdown"] <= 0.06},
        {"test": "Profitable weeks (at least 3 of the last 4)", "value": f"{sum(w['pnl'] > 0 for w in last4)} of {len(last4)}",
         "pass": None if len(last4) < 4 else sum(w["pnl"] > 0 for w in last4) >= 3},
    ]
    for t in tests:
        t["pass"] = None if t["pass"] is None else bool(t["pass"])
    today = date.today()
    passed = sum(t["pass"] is True for t in tests)
    if today < end:
        verdict = f"Trial running: day {(today - start).days + 1} of {RULES['trial_days']} (decision on {end.isoformat()})."
    else:
        verdict = "PASS: the rules have an edge on SPY. Try them on demo by hand next." if passed == len(tests) \
            else f"FAIL ({passed} of {len(tests)} tests passed): no proven edge. Don't trade these rules live."
    return {"tests": tests, "passed": passed, "weeks": weeks, "verdict": verdict}


def write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline-csv", help="test with saved 1-min bars instead of Alpaca")
    ap.add_argument("--start", help="trial start date (YYYY-MM-DD); default: first run")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    start_file = OUT / "scalp_start.json"
    if args.start:
        start = date.fromisoformat(args.start)
    elif start_file.exists():
        start = date.fromisoformat(json.loads(start_file.read_text())["date"])
    else:
        start = datetime.now(NY).date()
    start_file.write_text(json.dumps({"date": start.isoformat()}))
    end = start + timedelta(days=RULES["trial_days"])

    if args.offline_csv:
        raw, feed = pd.read_csv(args.offline_csv), "offline"
    else:
        now = datetime.now(timezone.utc) - timedelta(minutes=20)
        raw, feed = fetch_bars(datetime.combine(start - timedelta(days=RULES["backtest_days"] + 10), time(0), timezone.utc), now)
    bars, five = prepare(raw)
    today_ny = datetime.now(NY).date()
    days = [d for d in sorted(set(bars.index.date)) if d < today_ny or args.offline_csv]
    days = [d for d in days if len(bars[bars.index.date == d]) >= 300]     # skip half days and gaps
    bt_days = [d for d in days[1:] if d < start and d >= start - timedelta(days=RULES["backtest_days"])]
    tr_days = [d for d in days[1:] if d >= start]

    bt_trades, bt_hist = replay(bars, five, bt_days, RULES["account"])
    tr_trades, tr_hist = replay(bars, five, tr_days, RULES["account"])
    for t in bt_trades:
        t["phase"] = "backtest"
    for t in tr_trades:
        t["phase"] = "trial"

    bt, tr = stats(bt_trades, bt_hist, RULES["account"]), stats(tr_trades, tr_hist, RULES["account"])
    summary = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "symbol": RULES["symbol"], "feed": feed,
        "rules": {k: (v.isoformat() if isinstance(v, time) else v) for k, v in RULES.items() if k != "window"},
        "window_ny": "09:30-11:00 (A-C), to 12:30 (D)", "window_uk": "14:30-16:00 (A-C), to 17:30 (D)",
        "trial_start": start.isoformat(), "trial_end": end.isoformat(),
        "backtest": {**bt, "from": bt_days[0].isoformat() if bt_days else None,
                     "to": bt_days[-1].isoformat() if bt_days else None, "days": len(bt_days),
                     "weeks": weekly_results(bt_hist, RULES["account"])},
        "trial": {**tr, "days": len(tr_days)},
        "scorecard": scorecard(tr, tr_hist, RULES["account"], start, end),
    }
    write_csv(OUT / "scalp_trades.csv", bt_trades + tr_trades)
    write_csv(OUT / "scalp_history.csv", [{**h, "phase": "backtest"} for h in bt_hist] + [{**h, "phase": "trial"} for h in tr_hist])
    (OUT / "scalp_summary.json").write_text(json.dumps(summary, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    def line(name, s):
        if not s.get("trades"):
            return f"{name}: no trades yet"
        return (f"{name}: {s['trades']} trades, win rate {s['win_rate']:.0%}, average {s['avg_r']:+.2f}R, "
                f"return {s['return']:+.1%}, worst drop {s['max_drawdown']:.1%}")
    print(line(f"Backtest {len(bt_days)} days", bt))
    print(line(f"Trial {len(tr_days)} days", tr))
    print(summary["scorecard"]["verdict"])


if __name__ == "__main__":
    main()
