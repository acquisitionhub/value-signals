#!/usr/bin/env python3
"""
SHADOW MODE: two unsupervised models that watch alongside the system but never trade.

1. Odd accounting (autoencoder)
   A small neural network learns what "normal" company financials look like across the
   latest weekly screen (~300 large US companies) by squeezing 13 accounting ratios down
   to 3 numbers and rebuilding them. Companies it rebuilds badly are unusual. Unusual is
   not the same as fraud, but it is where fraud, collapses and accounting tricks live.

2. Market mood (clustering)
   Groups every trading day of the S&P 500 (trend, volatility, drawdown) into 3 moods:
   Calm, Choppy, Stressed. Today's mood suggests how much of the account to have invested.

How it improves over time
   - Both models are re-fitted from scratch on every run, on more data each time.
   - Market-mood results are walk-forward (out-of-sample): each past day is labelled by a model
     trained only on the days before it, so the track record can't flatter itself.
   - Market-mood history is saved and keeps growing (output/mood_history.csv), so after a
     year it has seen more market conditions than the 2-3 years of prices it starts with.
   - Every flag is logged (output/shadow_log.csv) and later checked against what the stock
     actually did vs the S&P 500. After 3 months that tells you whether to let it trade.

Nothing here changes a signal or places an order. Output: output/shadow.json (dashboard tab).
"""
import csv, json, math
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
OUT = HERE / "output"

SHADOW = {
    "odd_top_share": 0.05,      # flag the most unusual 5% of companies
    "odd_seeds": 5,             # average 5 networks so one unlucky start can't decide
    "odd_check_days": 30,       # judge a flag after 30+ calendar days
    # what the mood WOULD set as the maximum invested (the live limit stays in paper.py)
    "mood_exposure": {"Calm": 0.50, "Choppy": 0.35, "Stressed": 0.20},
}

# accounting ratio -> plain-English name used in the explanations
FEATURES = {
    "revenue_growth": "revenue growth",
    "revenue_cagr": "multi-year revenue growth",
    "net_margin": "net margin",
    "roic": "return on capital",
    "cash_conversion": "profit turning into cash",
    "eps_cagr": "earnings-per-share growth",
    "accruals": "accruals (profit not backed by cash)",
    "beneish": "Beneish M-Score (manipulation risk)",
    "altman": "Altman Z-Score (financial strength)",
    "piotroski_ratio": "Piotroski F-score (financial health)",
    "share_change_pa": "share count change",
    "fcf_trend": "free cash flow vs its 3-year average",
    "debt_to_fcf": "debt vs free cash flow",
}


# which direction is the WORRYING one for each ratio (+1 = high is bad, -1 = low is bad)
BAD_DIRECTION = {"beneish": 1, "accruals": 1, "debt_to_fcf": 1, "share_change_pa": 1, "eps_cagr": 1,
                 "altman": -1, "cash_conversion": -1, "piotroski_ratio": -1, "net_margin": -1,
                 "roic": -1, "fcf_trend": -1, "revenue_growth": -1, "revenue_cagr": -1}


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# 1. ODD ACCOUNTING
# --------------------------------------------------------------------------------------
def load_screen():
    screens = sorted(OUT.glob("screen_*.csv"))
    if not screens:
        return None, None
    df = pd.read_csv(screens[-1])
    fcf_l, fcf_a = df["fcf_latest"].map(num), df["fcf_avg3"].map(num)
    df["fcf_trend"] = [(l - a) / abs(a) if l is not None and a not in (None, 0) else None
                       for l, a in zip(fcf_l, fcf_a)]
    df["debt_to_fcf"] = [d / a if a and a > 0 and d is not None else (20.0 if a is not None and a <= 0 else None)
                         for d, a in zip(df["debt_latest"].map(num), fcf_a)]
    df["piotroski_ratio"] = [p / o if o else None for p, o in zip(df["piotroski"].map(num), df["piotroski_of"].map(num))]
    return df, screens[-1].stem.replace("screen_", "")


def odd_accounting(df):
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import QuantileTransformer

    cols = list(FEATURES)
    X = df[cols].apply(pd.to_numeric, errors="coerce")
    present = X.notna().mean(axis=1)
    keep = present >= 0.7                      # need most of the numbers to judge
    X, base = X[keep], df[keep].reset_index(drop=True)
    X = X.reset_index(drop=True)
    missing = X.isna()
    X = X.fillna(X.median())
    # rank-based scaling: one crazy number can't dominate, and every ratio counts equally
    qt = QuantileTransformer(n_quantiles=min(100, len(X)), output_distribution="normal", random_state=0)
    Z = np.clip(qt.fit_transform(X), -3, 3)

    errs, recon = np.zeros_like(Z), np.zeros_like(Z)
    for seed in range(SHADOW["odd_seeds"]):
        ae = MLPRegressor(hidden_layer_sizes=(8, 3, 8), activation="tanh", alpha=1e-3,
                          max_iter=4000, random_state=seed)
        ae.fit(Z, Z)                           # learn to rebuild its own input through a 3-number squeeze
        R = ae.predict(Z)
        recon += R / SHADOW["odd_seeds"]
    errs = (Z - recon) ** 2
    errs[missing.values] = 0                   # don't blame a company for numbers we filled in
    score = errs.sum(axis=1) / np.maximum((~missing.values).sum(axis=1), 1)
    pct = pd.Series(score).rank(pct=True).values

    n_flag = max(3, int(round(len(score) * SHADOW["odd_top_share"])))
    order = np.argsort(-score)
    rows = []
    for rank, i in enumerate(order):
        top = np.argsort(-errs[i])[:2]
        reasons, concern = [], False
        for j in top:
            if errs[i, j] < 0.5:
                continue
            high = Z[i, j] > recon[i, j]
            bad = BAD_DIRECTION[cols[j]] == (1 if high else -1)
            concern |= bad
            raw = X.iloc[i, j]
            reasons.append(("⚠ " if bad else "") + f"{FEATURES[cols[j]]} {'unusually high' if high else 'unusually low'} "
                           f"({fmt(cols[j], raw)})")
        rows.append({"ticker": base.loc[i, "ticker"], "name": base.loc[i, "name"],
                     "score": round(float(score[i]), 3), "percentile": round(float(pct[i]), 3),
                     "flagged": rank < n_flag, "concern": concern, "reasons": reasons,
                     "price": num(base.loc[i, "price"]), "passes": str(base.loc[i, "passes"]) == "True"})
    return rows, int(keep.sum()), int((~keep).sum())


def fmt(col, v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if col in ("beneish", "altman", "debt_to_fcf"):
        return f"{v:.1f}" + ("x" if col == "debt_to_fcf" else "")
    return f"{v*100:+.0f}%" if col in ("revenue_growth", "revenue_cagr", "eps_cagr", "share_change_pa", "fcf_trend") \
        else f"{v*100:.0f}%"


# --------------------------------------------------------------------------------------
# 2. MARKET MOOD
# --------------------------------------------------------------------------------------
MOOD_FILE = OUT / "mood_history.csv"


def spy_history():
    import prices
    h = prices.history("SPY")
    if h is None:
        return None
    s = h["Close"].astype(float)
    # merge with what earlier runs saved, so the history keeps growing beyond the 2-3 year download
    if MOOD_FILE.exists():
        old = pd.read_csv(MOOD_FILE, index_col=0, parse_dates=True)["close"]
        s = pd.concat([old[old.index < s.index[0]], s])
    s = s[~s.index.duplicated(keep="last")].sort_index()
    pd.DataFrame({"close": s}).to_csv(MOOD_FILE)
    return s


def mood_features(s):
    r = s.pct_change()
    f = pd.DataFrame({
        "trend_20d": s / s.shift(20) - 1,
        "trend_60d": s / s.shift(60) - 1,
        "vol_20d": r.rolling(20).std() * math.sqrt(252),
        "vs_200d": s / s.rolling(200).mean() - 1,
        "drawdown": s / s.rolling(252, min_periods=60).max() - 1,
    })
    return f.dropna()


def fit_moods(F):
    """Fit the 3-group model on F; return (predict function, names) with groups named by what they look like."""
    from sklearn.mixture import GaussianMixture
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler().fit(F)
    Z = sc.transform(F)
    gm = GaussianMixture(n_components=3, covariance_type="full", n_init=5, random_state=0).fit(Z)
    lab = gm.predict(Z)
    zf = pd.DataFrame(Z, columns=F.columns)
    # group numbers can swap between refits, so name each group by its own character
    stress = {k: (zf[lab == k]["vol_20d"].mean() - zf[lab == k]["drawdown"].mean()
                  - zf[lab == k]["trend_60d"].mean()) if (lab == k).any() else 0 for k in range(3)}
    names = dict(zip(sorted(stress, key=stress.get), ["Calm", "Choppy", "Stressed"]))
    predict = lambda G: [names[k] for k in gm.predict(sc.transform(G))]
    proba = lambda G: gm.predict_proba(sc.transform(G))
    return predict, proba


def walk_forward(F, train_min=252, step=21):
    """Honest labels: each day is judged by a model trained ONLY on days before it.
    Expanding window, refit every `step` trading days (about monthly)."""
    out = []
    for start in range(train_min, len(F), step):
        predict, _ = fit_moods(F.iloc[:start])
        chunk = F.iloc[start:start + step]
        out += list(zip(chunk.index, predict(chunk)))
    return pd.Series(dict(out)) if out else pd.Series(dtype=object)


def market_mood(s):
    F = mood_features(s)
    if len(F) < 120:
        return None
    # today's mood: model trained on everything up to today (uses no future data)
    predict, proba = fit_moods(F)
    mood = pd.Series(predict(F), index=F.index)
    today = mood.iloc[-1]
    since = mood.index[-1]
    for d, m in zip(reversed(mood.index), reversed(mood.values)):
        if m != today:
            break
        since = d
    conf = float(max(proba(F.iloc[-1:])[0]))

    # track record: OUT-OF-SAMPLE only (walk-forward), how the S&P did over the next 20 trading days
    oos = walk_forward(F)
    fwd = s.shift(-20) / s - 1
    stats = {}
    for m in ["Calm", "Choppy", "Stressed"]:
        idx = oos.index[oos == m]
        fr = fwd.reindex(idx).dropna()
        stats[m] = {"days": int(len(idx)), "share": round(len(idx) / len(oos), 3) if len(oos) else 0,
                    "next20_avg": round(float(fr.mean()), 4) if len(fr) else None,
                    "next20_worst": round(float(fr.min()), 4) if len(fr) else None,
                    "next20_down_share": round(float((fr < 0).mean()), 3) if len(fr) else None}
    allf = fwd.reindex(oos.index).dropna()
    baseline = {"next20_avg": round(float(allf.mean()), 4) if len(allf) else None,
                "next20_down_share": round(float((allf < 0).mean()), 3) if len(allf) else None,
                "next20_worst": round(float(allf.min()), 4) if len(allf) else None}
    last = F.iloc[-1]
    recent = [{"d": d.strftime("%Y-%m-%d"), "m": m} for d, m in mood.iloc[-60:].items()]
    return {"today": today, "since": since.strftime("%Y-%m-%d"), "confidence": round(conf, 2),
            "exposure": SHADOW["mood_exposure"][today],
            "now": {k: round(float(v), 4) for k, v in last.items()},
            "stats": stats, "baseline": baseline, "oos_days": int(len(oos)),
            "oos_from": oos.index[0].strftime("%Y-%m-%d") if len(oos) else None,
            "history_days": int(len(F)), "history_from": F.index[0].strftime("%Y-%m-%d"), "recent": recent}


# --------------------------------------------------------------------------------------
# LOG + SELF-CHECK
# --------------------------------------------------------------------------------------
LOG = OUT / "shadow_log.csv"
LOG_COLS = ["date", "kind", "ticker", "value", "price", "spy"]


def read_log():
    if not LOG.exists():
        return []
    with LOG.open() as f:
        return list(csv.DictReader(f))


def write_log(new_rows):
    exists = LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(LOG_COLS)
        w.writerows(new_rows)


def check_past_flags(log, spy_now):
    """Did flagged companies lag the S&P 500 afterwards? (The test that decides go-live.)"""
    import prices
    first = {}
    for r in log:                             # judge each company from its FIRST flag
        if r["kind"] == "odd" and r["ticker"] not in first:
            first[r["ticker"]] = r
    due = [r for r in first.values()
           if (date.today() - datetime.strptime(r["date"], "%Y-%m-%d").date()).days >= SHADOW["odd_check_days"]
           and num(r["price"]) and num(r["spy"])]
    if not due or not spy_now:
        return {"checked": 0, "note": f"First results once flags are {SHADOW['odd_check_days']}+ days old."}
    now = prices.latest_many([r["ticker"] for r in due])
    rel = []
    for r in due:
        p = now.get(r["ticker"])
        if p:
            rel.append({"ticker": r["ticker"], "since": r["date"],
                        "vs_spy": round((p / num(r["price"])) / (spy_now / num(r["spy"])) - 1, 4)})
    if not rel:
        return {"checked": 0, "note": "No prices available to check the flags yet."}
    v = [x["vs_spy"] for x in rel]
    return {"checked": len(v), "avg_vs_spy": round(float(np.mean(v)), 4),
            "lagged_share": round(float(np.mean([x < 0 for x in v])), 3),
            "worst": sorted(rel, key=lambda x: x["vs_spy"])[:5]}


def main():
    OUT.mkdir(exist_ok=True)
    today = date.today().isoformat()
    result = {"updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "settings": SHADOW}

    spy = None
    try:
        s = spy_history()
        if s is not None:
            spy = float(s.iloc[-1])
            result["mood"] = market_mood(s)
    except Exception as e:
        result["mood_error"] = str(e)[:200]

    log = read_log()
    logged_today = {(r["kind"], r["ticker"]) for r in log if r["date"] == today}
    new = []
    try:
        df, screen_date = load_screen()
        if df is not None:
            rows, n_used, n_skipped = odd_accounting(df)
            result["odd"] = {"screen_date": screen_date, "companies": n_used, "skipped_missing_data": n_skipped,
                             "flagged": [r for r in rows if r["flagged"]],
                             "all": [{k: r[k] for k in ("ticker", "score", "percentile")} for r in rows]}
            for r in rows:
                if r["flagged"] and ("odd", r["ticker"]) not in logged_today:
                    new.append([today, "odd", r["ticker"], r["score"], r["price"], spy])
    except Exception as e:
        result["odd_error"] = str(e)[:200]

    if result.get("mood") and ("mood", "SPY") not in logged_today:
        new.append([today, "mood", "SPY", result["mood"]["today"], spy, spy])
    if new:
        write_log(new)
        log += [dict(zip(LOG_COLS, map(str, r))) for r in new]

    try:
        result["track_record"] = check_past_flags(log, spy)
    except Exception as e:
        result["track_record"] = {"checked": 0, "note": f"check failed: {str(e)[:120]}"}

    (OUT / "shadow.json").write_text(json.dumps(result, indent=1, default=str))
    m = result.get("mood")
    print(f"Shadow: mood {m['today'] if m else 'n/a'}; "
          f"{len(result.get('odd', {}).get('flagged', []))} odd-accounting flags")


if __name__ == "__main__":
    main()
