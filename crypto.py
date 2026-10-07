"""
Crypto data for the RSI 50 Scanner.

Prices:   Binance public market data (data-api.binance.vision), daily candles, no key needed.
Leverage: Hyperliquid perps (api.hyperliquid.xyz), current open interest and funding, no key needed.
          Open interest history builds up in derivs_history.json, one snapshot per run.

Liquidation cascades (e.g. 12 Mar 2020, 19 May 2021, 5 Aug 2024, 10 Oct 2025) show up as
deep wicks on most coins on the same day, so the market meter tracks the median wick:
how far each coin's low fell below the previous close.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent

# ----------------------------------------------------------------------
# SETTINGS
# ----------------------------------------------------------------------
CRYPTO_TOP_N = 150            # coins by 24h volume on Binance (USDT pairs)
CRYPTO_START = "2019-01-01"   # history start, covers the Covid crash
CRYPTO_WASH_DROP = -25        # % fall from 20-day high for a coin to count as flushing
WICK_STRESS = 6               # median wick depth % = stressed day
WICK_EVENT = 12               # median wick depth % = liquidation cascade
OI_FLUSH = -15                # % drop in open interest over 3 days = leverage flushed
DERIVS_KEEP_DAYS = 45
# ----------------------------------------------------------------------

BINANCE = "https://data-api.binance.vision/api/v3"
HYPER = "https://api.hyperliquid.xyz/info"

SKIP = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "USD1", "USDE", "PAXG",
        "XUSD", "AEUR", "EURI", "BFUSD", "RLUSD", "USDS", "PYUSD", "WBTC", "WBETH", "BNSOL", "XAUT"}


def _get(url, **params):
    import requests
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(10)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 3)


def top_symbols():
    rows = _get(f"{BINANCE}/ticker/24hr")
    out = []
    for t in rows:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in SKIP or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        out.append((float(t["quoteVolume"]), base))
    out.sort(reverse=True)
    return [b for _, b in out[:CRYPTO_TOP_N]]


def klines(base):
    start = int(pd.Timestamp(CRYPTO_START, tz="UTC").timestamp() * 1000)
    rows = []
    while True:
        batch = _get(f"{BINANCE}/klines", symbol=f"{base}USDT", interval="1d", startTime=start, limit=1000)
        if not batch:
            break
        rows += batch
        if len(batch) < 1000:
            break
        start = batch[-1][0] + 86_400_000
        time.sleep(0.15)
    if not rows:
        return None
    now_ms = time.time() * 1000
    rows = [r for r in rows if r[6] < now_ms]          # drop today's unfinished candle
    df = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "Volume", "ct", "qv", "n", "tb", "tq", "x"])
    df.index = pd.to_datetime(df["t"], unit="ms")
    return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)


def download():
    print("  crypto: loading top coins from Binance")
    names = top_symbols()
    data = {}
    for i, b in enumerate(names):
        try:
            df = klines(b)
            if df is not None and len(df) > 60:
                data[f"{b}-USD"] = df
        except Exception as e:
            print(f"    ! {b}: {e}")
        if i % 25 == 24:
            print(f"    {i + 1}/{len(names)}")
        time.sleep(0.1)
    print(f"  crypto: {len(data)} coins")
    return data


# ----------------------------------------------------------------------
# Leverage (Hyperliquid)
# ----------------------------------------------------------------------

def derivatives():
    """Current funding + open interest per coin, and change vs saved snapshots."""
    import requests
    try:
        meta, ctxs = requests.post(HYPER, json={"type": "metaAndAssetCtxs"}, timeout=30).json()
    except Exception as e:
        print(f"  ! Hyperliquid unavailable: {e}")
        return {}, None
    now = {}
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        try:
            name = u["name"]
            px = float(c.get("markPx") or c.get("oraclePx"))
            now[name] = {"oi": float(c["openInterest"]) * px, "fund": float(c["funding"])}
        except Exception:
            continue
    return now, datetime.now(timezone.utc).strftime("%Y-%m-%d")


def attach_derivs(assets, snap, day, history_file=ROOT / "derivs_history.json"):
    hist = {}
    if history_file.exists():
        try:
            hist = json.loads(history_file.read_text())
        except Exception:
            hist = {}
    if snap:
        hist[day] = {k: round(v["oi"]) for k, v in snap.items()}
        keep = sorted(hist)[-DERIVS_KEEP_DAYS:]
        hist = {k: hist[k] for k in keep}
        history_file.write_text(json.dumps(hist, separators=(",", ":")))
    days = sorted(hist)

    def back(n):
        if not day:
            return {}
        target = (pd.Timestamp(day) - pd.Timedelta(days=n)).strftime("%Y-%m-%d")
        older = [d for d in days if d <= target]
        return hist[older[-1]] if older else {}

    d1, d3 = back(1), back(3)
    total = {"now": 0, "d1": 0, "d3": 0}
    neg = cnt = 0
    for t, a in assets.items():
        if not t.endswith("-USD"):
            continue
        base = t[:-4]
        key = base if base in (snap or {}) else ("k" + base[4:] if base.startswith("1000") else "k" + base)
        s = (snap or {}).get(key)
        if not s:
            continue
        oi = s["oi"]

        def chg(old):
            o = old.get(key)
            return round((oi / o - 1) * 100, 1) if o else None

        a["deriv"] = {"oi": round(oi), "fund_apr": round(s["fund"] * 24 * 365 * 100, 1),
                      "oi1": chg(d1), "oi3": chg(d3)}
        cnt += 1
        neg += s["fund"] < 0
        if a["deriv"]["oi3"] is not None:
            total["now"] += oi
            total["d3"] += d3[key]
        if a.get("wash") and a["deriv"]["oi3"] is not None and a["deriv"]["oi3"] <= OI_FLUSH:
            a["wash"]["triggers"].append(f"Leverage flushed (OI {a['deriv']['oi3']}% in 3 days)")
    return {
        "neg_funding": round(neg / cnt * 100) if cnt else None,
        "oi3": round((total["now"] / total["d3"] - 1) * 100, 1) if total["d3"] else None,
        "days": len(days),
    }


# ----------------------------------------------------------------------
# Market meter
# ----------------------------------------------------------------------

def build_market(frames, rsis, r):
    """frames: {coin: df} liquid coins. Median wick + % oversold per day, and past cascades."""
    if not frames:
        return None
    lows = pd.DataFrame({t: d["Low"] for t, d in frames.items()})
    prev = pd.DataFrame({t: d["Close"].shift(1) for t, d in frames.items()})
    wick = ((lows / prev - 1) * -100).clip(lower=0)            # positive % depth
    count = wick.notna().sum(axis=1)
    ok = count >= 15
    med = wick.median(axis=1)[ok].iloc[1:]
    rs = pd.DataFrame(rsis).reindex(med.index)
    oversold = (rs.lt(30).sum(axis=1) / rs.notna().sum(axis=1) * 100).fillna(0)

    btc = frames.get("BTC-USD")
    btc_c = btc["Close"] if btc is not None else None
    btc_wick = wick["BTC-USD"] if "BTC-USD" in wick else None

    events, cur = [], None
    for day, v in med.items():
        if v >= WICK_EVENT:
            if cur and (day - cur["last"]).days <= 14:
                cur["last"] = day
                if v > cur["peak"]:
                    cur.update(peak=v, date=day)
            else:
                cur = {"date": day, "peak": v, "last": day}
                events.append(cur)
    ev = []
    for e in events:
        d = e["date"]
        row = {"date": d.strftime("%Y-%m-%d"), "peak": r(e["peak"], 1),
               "oversold": r(oversold.get(d), 1), "coins": int(count.get(d, 0))}
        if btc_c is not None and d in btc_c.index:
            s = btc_c.loc[:d]
            row["btc_dd"] = r((btc["Low"].loc[d] / s.iloc[-60:].max() - 1) * 100, 1)
            row["btc_wick"] = r(-btc_wick.loc[d], 1) if btc_wick is not None else None
            after = btc_c.loc[d:]
            for k, n in (("m1", 30), ("m3", 90), ("m6", 180)):
                row[k] = r((after.iloc[n] / after.iloc[0] - 1) * 100, 1) if len(after) > n else None
        ev.append(row)

    weekly = med.resample("W-SUN").max().dropna()
    return {
        "now": r(med.iloc[-1], 1), "date": med.index[-1].strftime("%Y-%m-%d"),
        "oversold": r(oversold.iloc[-1], 1), "worst7": r(med.iloc[-7:].max(), 1),
        "stress": WICK_STRESS, "event": WICK_EVENT,
        "hist_dates": [d.strftime("%Y-%m-%d") for d in weekly.index],
        "hist": [r(v, 1) for v in weekly],
        "events": ev[::-1], "stocks": int(count.iloc[-1]),
    }


# ----------------------------------------------------------------------
# Demo
# ----------------------------------------------------------------------

def demo_data():
    rng = np.random.default_rng(11)
    idx = pd.date_range(CRYPTO_START, pd.Timestamp.today().normalize() - pd.Timedelta(days=1), freq="D")
    n = len(idx)
    crash = {pd.Timestamp("2020-03-12"): (0.38, 0.30), pd.Timestamp("2021-05-19"): (0.30, 0.12),
             pd.Timestamp("2022-06-13"): (0.18, 0.12), pd.Timestamp("2024-08-05"): (0.20, 0.08),
             pd.Timestamp("2025-10-10"): (0.45, 0.10)}
    coins = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "SUI", "DOT", "LTC",
             "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "SEI", "PEPE", "WIF", "BONK", "FET", "RNDR",
             "AAVE", "UNI", "ATOM", "HBAR", "TON", "TRX", "ENA", "ONDO", "JUP", "PYTH", "STX", "IMX",
             "FIL", "ICP", "ETC", "BCH"]
    data, snap = {}, {}
    for i, c in enumerate(coins):
        start = 0 if i < 15 else int(rng.integers(300, 1500))
        ret = rng.normal(0.0008, 0.04 if c != "BTC" else 0.028, n)
        ret += 0.01 * np.sin(np.arange(n) / rng.uniform(40, 160))
        wick = np.abs(rng.normal(0, 0.02, n))
        for d, (w, cl) in crash.items():
            if d in idx:
                j = idx.get_loc(d)
                beta = 0.6 if c == "BTC" else rng.uniform(0.9, 1.6)
                wick[j] = w * beta
                ret[j] -= cl * beta
                ret[j + 1:j + 4] += 0.03
        if i % 9 == 4:                                     # a few flushing right now
            ret[-6:-1] -= 0.08
            ret[-1] = 0.07 if i % 2 else -0.03
        px = 50 * np.exp(np.cumsum(ret))
        close = pd.Series(px, idx)
        prev = close.shift(1).fillna(close)
        low = np.minimum(close, prev) * (1 - np.clip(wick, 0, 0.9))
        high = np.maximum(close, prev) * (1 + np.abs(rng.normal(0, 0.015, n)))
        vol = rng.uniform(2e5, 2e6, n) * (1 + 6 * (wick > 0.1))
        if i % 9 == 4:
            vol[-5:] *= 5
        df = pd.DataFrame({"Open": prev, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx).iloc[start:]
        data[f"{c}-USD"] = df
        snap[c] = {"oi": rng.uniform(5e7, 2e9), "fund": rng.normal(0, 0.00002)}
    meta = {t: {"name": t[:-4], "index": ["crypto"]} for t in data}
    return meta, data, snap
