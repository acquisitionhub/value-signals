"""
Daily price history. Uses Alpaca's free market-data API when ALPACA keys are set
(reliable from GitHub's cloud servers), otherwise Yahoo Finance via yfinance.
"""
import os
from datetime import date, timedelta

import pandas as pd
import requests


def _alpaca_bars(symbol, years=3):
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        return None
    base = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
    params = {"timeframe": "1Day", "start": (date.today() - timedelta(days=365 * years)).isoformat(),
              "limit": 10000, "adjustment": "all", "feed": os.environ.get("ALPACA_FEED", "iex")}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    bars = []
    while True:
        r = requests.get(f"{base}/v2/stocks/{symbol}/bars", params=params, headers=headers, timeout=30)
        if r.status_code >= 400:
            return None
        j = r.json()
        bars += j.get("bars") or []
        if not j.get("next_page_token"):
            break
        params["page_token"] = j["next_page_token"]
    if not bars:
        return None
    df = pd.DataFrame(bars)
    df.index = pd.to_datetime(df["t"]).dt.tz_localize(None).dt.normalize()
    return df.rename(columns={"c": "Close", "v": "Volume"})[["Close", "Volume"]]


def _yahoo(symbol):
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(period="2y", auto_adjust=True)
        return h[["Close", "Volume"]] if len(h) else None
    except Exception:
        return None


def history(symbol):
    """~2-3 years of daily Close/Volume, or None."""
    h = None
    try:
        h = _alpaca_bars(symbol)
    except Exception:
        h = None
    if h is None or len(h) < 220:
        h = _yahoo(symbol)
    return h if h is not None and len(h) > 220 else None


def latest(symbol):
    h = None
    try:
        h = _alpaca_bars(symbol, years=0.05)
    except Exception:
        pass
    if h is None or not len(h):
        h = _yahoo(symbol)
    return float(h["Close"].iloc[-1]) if h is not None and len(h) else None
