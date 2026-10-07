"""
RSI 50 Scanner
--------------
Scans US + UK stocks once a day for three RSI(14) setups on the daily,
weekly and monthly timeframes, and writes the results to data.json for
the dashboard (index.html) to display.

Setups:
  TAP      RSI is sitting right on the 50 line (48-52)
  BOUNCE   RSI touched ~50 recently and is now pushing up into 55-60
  RECLAIM  RSI was well below 50 recently and has just crossed back above it

Run:  python scanner.py          (real data from Yahoo Finance)
      python scanner.py --demo   (fake data, for testing the dashboard)
"""

import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent

# ----------------------------------------------------------------------
# SETTINGS - change these to tune the scanner
# ----------------------------------------------------------------------

RSI_PERIOD = 14

# Which markets to scan. Remove any you don't want.
# (Wikipedia's Nasdaq-100 page no longer lists its members. Almost all are in the
#  S&P 500; the few that aren't are in extra_tickers.txt.)
UNIVERSES = ["sp500", "ftse100", "ftse250"]

# Extra tickers to always scan (Yahoo format, e.g. "PLTR", "RR.L").
# You can also list them one per line in extra_tickers.txt
EXTRA_TICKERS = []

# Skip illiquid stocks: minimum average daily traded value over 20 days,
# in the stock's own currency (US$ for US stocks, £ for UK stocks).
MIN_AVG_DAILY_VALUE = 2_000_000

# Weekly / monthly: only use CLOSED candles (True) or include the
# candle that is still forming (False). Closed = fewer, cleaner signals.
CLOSED_CANDLES_ONLY = True

# Setup thresholds
TAP_LOW, TAP_HIGH = 48, 52              # "tapping 50" band
BOUNCE_LOW, BOUNCE_HIGH = 55, 60        # where a bounce should be heading
BOUNCE_TOUCH_LOW, BOUNCE_TOUCH_HIGH = 46, 53   # recent low must have been near 50
RECLAIM_DIP = 45                        # must have been below this recently

# How many bars back to look for the "recent" part of each setup
LOOKBACK = {"D": 10, "W": 8, "M": 6}

HISTORY_POINTS = 30                     # RSI points kept for the dashboard sparkline

# Washouts tab (capitulation / liquidation-style flushes)
WASH_DROP = -15          # % fall from the 20-day high to the recent low
WASH_RSI = 25            # daily RSI must have hit this or lower in the last WASH_WINDOW bars
WASH_VOL = 2.5           # a day in the window with volume this many times the 50-day average
WASH_WINDOW = 5          # bars
MARKET_OVERSOLD_RSI = 30     # a stock counts as "oversold" for market breadth below this
MARKET_STRESS = 10           # % of stocks oversold = stressed market
MARKET_EVENT = 25            # % of stocks oversold = market-wide liquidation event
HISTORY_YEARS = 10           # years of daily data (covers Covid 2020)

# Crypto (top coins on Binance + leverage data from Hyperliquid). Settings in crypto.py
INCLUDE_CRYPTO = True

# ----------------------------------------------------------------------


def wilder_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Standard RSI (Wilder's smoothing) - matches TradingView."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    rsi[avg_loss == 0] = 100
    return rsi


def to_weekly(close: pd.Series, crypto=False) -> pd.Series:
    # stocks: week ends Friday. crypto: week ends Sunday (UTC)
    end = 6 if crypto else 4
    w = close.resample("W-SUN" if crypto else "W-FRI").last().dropna()
    if CLOSED_CANDLES_ONLY and len(close) and close.index[-1].weekday() != end:
        w = w.iloc[:-1]          # this week's candle hasn't closed yet
    return w


def to_monthly(close: pd.Series, crypto=False) -> pd.Series:
    m = close.resample("ME").last().dropna()
    if CLOSED_CANDLES_ONLY and len(close):
        last = close.index[-1]
        month_end = pd.offsets.MonthEnd().rollforward(last) if crypto else pd.offsets.BMonthEnd().rollforward(last)
        if last != month_end:
            m = m.iloc[:-1]      # this month's candle hasn't closed yet
    return m


def classify(rsi: pd.Series, tf: str):
    """Return the setup name for the latest bar, or None."""
    rsi = rsi.dropna()
    lb = LOOKBACK[tf]
    if len(rsi) < lb + 2:
        return None
    now, prev = rsi.iloc[-1], rsi.iloc[-2]
    recent = rsi.iloc[-(lb + 1):-1]       # the bars before this one

    # RECLAIM: just crossed up through 50 after being properly below it
    if prev < 50 <= now and recent.min() < RECLAIM_DIP:
        return "RECLAIM"

    # BOUNCE: came down to ~50, held, now rising into 55-60
    if (BOUNCE_LOW <= now <= BOUNCE_HIGH and now > prev
            and BOUNCE_TOUCH_LOW <= recent.min() <= BOUNCE_TOUCH_HIGH):
        return "BOUNCE"

    # TAP: sitting on the line right now
    if TAP_LOW <= now <= TAP_HIGH:
        return "TAP"
    return None


def washout(close, high, low, vol, rsi_d, max_drop=None):
    """Capitulation check on daily bars: sharp fall + deeply oversold + volume spike,
    then whether a reversal trigger printed on the latest bar."""
    n = WASH_WINDOW
    if len(close) < 60 or rsi_d.dropna().empty:
        return None
    hi20 = high.iloc[-20:].max()
    lo = low.iloc[-n:].min()
    drop = (lo / hi20 - 1) * 100
    rsi_low = rsi_d.iloc[-n:].min()
    base = vol.iloc[-(50 + n):-n].mean()
    volx = (vol.iloc[-n:].max() / base) if base > 0 else 0
    if not (drop <= (max_drop or WASH_DROP) and rsi_low <= WASH_RSI and volx >= WASH_VOL):
        return None

    c, h, l = close.iloc[-1], high.iloc[-1], low.iloc[-1]
    o_prev_high = high.iloc[-2]
    rng = h - l
    body_low = min(c, close.iloc[-2])
    trig = []
    if c > o_prev_high:
        trig.append("Closed above yesterday's high")
    if rng > 0 and (c - l) / rng >= 0.66 and (body_low - l) >= 0.5 * rng:
        trig.append("Hammer / long lower wick")
    if rsi_d.iloc[-2] < 30 <= rsi_d.iloc[-1]:
        trig.append("RSI back above 30")
    return {
        "status": "REVERSAL" if trig else "FLUSHING",
        "drop": r(drop, 1), "rsi_low": r(rsi_low, 1), "volx": r(volx, 1),
        "off_low": r((c / lo - 1) * 100, 1), "rsi": r(rsi_d.iloc[-1], 1),
        "triggers": trig,
    }


def r(x, nd=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


def analyse(ticker: str, df: pd.DataFrame, meta: dict):
    close = df["Close"].dropna()
    if len(close) < 60:
        return None
    vol = df["Volume"].reindex(close.index).fillna(0)
    high = df["High"].reindex(close.index).fillna(close) if "High" in df else close
    low = df["Low"].reindex(close.index).fillna(close) if "Low" in df else close

    value = (close * vol).tail(20).mean()
    ccy = meta.get("ccy") or ("GBp" if ticker.endswith(".L") else "USD")
    if ccy == "GBp":
        value /= 100            # most UK prices are in pence
    liquid = bool(value >= MIN_AVG_DAILY_VALUE)

    crypto = ticker.endswith("-USD")
    frames = {"D": close, "W": to_weekly(close, crypto), "M": to_monthly(close, crypto)}
    tfs = {}
    for tf, series in frames.items():
        rsi = wilder_rsi(series)
        valid = rsi.dropna()
        if valid.empty:
            tfs[tf] = None
            continue
        tfs[tf] = {
            "rsi": r(valid.iloc[-1]),
            "prev": r(valid.iloc[-2]) if len(valid) > 1 else None,
            "signal": classify(rsi, tf),
            "bar": valid.index[-1].strftime("%Y-%m-%d"),
            "hist": [r(v, 1) for v in valid.tail(HISTORY_POINTS)],
        }

    rsi_d = wilder_rsi(close)
    from crypto import CRYPTO_WASH_DROP
    wash = washout(close, high, low, vol, rsi_d, CRYPTO_WASH_DROP if crypto else None)

    price = close.iloc[-1]
    prev_close = close.iloc[-2]
    return {
        "name": meta.get("name", ticker),
        "index": meta.get("index", []),
        "ccy": ccy,
        "type": "crypto" if crypto else ("uk" if ticker.endswith(".L") else "us"),
        "price": float(f"{price:.6g}"),     # 6 significant figures, so tiny coin prices survive
        "chg": r((price / prev_close - 1) * 100),
        "date": close.index[-1].strftime("%Y-%m-%d"),
        "liquid": liquid,
        "tf": tfs,
        "wash": wash,
    }, rsi_d


# ----------------------------------------------------------------------
# Ticker lists
# ----------------------------------------------------------------------

def _wiki(url):
    import io, requests
    html = requests.get(url, headers={"User-Agent": "Mozilla/5.0 rsi-scanner"}, timeout=30).text
    return pd.read_html(io.StringIO(html))


def _pick(tables, sym_cols, name_cols):
    for t in tables:
        cols = [str(c) for c in t.columns]
        s = next((c for c in cols if c in sym_cols), None)
        n = next((c for c in cols if c in name_cols), None)
        if s and len(t) > 50:
            t.columns = cols
            return t[s].astype(str).tolist(), (t[n].astype(str).tolist() if n else t[s].astype(str).tolist())
    raise ValueError("table not found")


def uk_currencies(tickers):
    """Most London prices are in pence (GBp), but some trade in GBP or USD.
    Look each one up once and remember it in ccy_cache.json."""
    import yfinance as yf
    f = ROOT / "ccy_cache.json"
    cache = json.loads(f.read_text()) if f.exists() else {}
    todo = [t for t in tickers if t not in cache]
    if todo:
        print(f"  checking currency for {len(todo)} UK tickers")
    for t in todo:
        try:
            cache[t] = yf.Ticker(t).fast_info["currency"] or "GBp"
        except Exception:
            pass
    f.write_text(json.dumps(cache, indent=0, sort_keys=True))
    odd = {t: c for t, c in cache.items() if c != "GBp"}
    if odd:
        print(f"  UK tickers not in pence: {odd}")
    return cache


def load_universe():
    meta = {}

    def add(tick, name, idx):
        tick = tick.strip().upper()
        if not tick or tick == "NAN":
            return
        m = meta.setdefault(tick, {"name": name, "index": []})
        if idx not in m["index"]:
            m["index"].append(idx)

    sources = {
        "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                  ["Symbol"], ["Security"], lambda t: t.replace(".", "-")),
        "nasdaq100": ("https://en.wikipedia.org/wiki/Nasdaq-100",
                      ["Ticker", "Symbol"], ["Company", "Security"], lambda t: t.replace(".", "-")),
        "ftse100": ("https://en.wikipedia.org/wiki/FTSE_100_Index",
                    ["Ticker", "EPIC"], ["Company"], lambda t: t.rstrip(".").replace(".", "-") + ".L"),
        "ftse250": ("https://en.wikipedia.org/wiki/FTSE_250_Index",
                    ["Ticker", "EPIC", "Ticker symbol"], ["Company"], lambda t: t.rstrip(".").replace(".", "-") + ".L"),
    }
    for key in UNIVERSES:
        url, sc, nc, fix = sources[key]
        try:
            syms, names = _pick(_wiki(url), sc, nc)
            for s, n in zip(syms, names):
                add(fix(s), n, key)
            print(f"  {key}: {len(syms)} tickers")
        except Exception as e:
            print(f"  ! could not load {key}: {e}")

    extras = list(EXTRA_TICKERS)
    f = ROOT / "extra_tickers.txt"
    if f.exists():
        extras += [l.split("#")[0].strip() for l in f.read_text().splitlines()]
    for t in extras:
        if t:
            add(t, t, "extra")
    return meta


# ----------------------------------------------------------------------
# Data download
# ----------------------------------------------------------------------

def download(tickers, chunk=100):
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        print(f"  downloading {i + 1}-{i + len(batch)} of {len(tickers)}")
        for attempt in range(3):
            try:
                data = yf.download(batch, period=f"{HISTORY_YEARS}y", interval="1d", auto_adjust=True,
                                   group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:
                print(f"    retry ({e})")
                time.sleep(5)
        else:
            continue
        for t in batch:
            try:
                df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                df = df.dropna(how="all")
                if not df.empty:
                    out[t] = df
            except KeyError:
                pass
        time.sleep(1)
    return out


def demo_data():
    """Synthetic prices so the dashboard can be tested without internet."""
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=252 * HISTORY_YEARS)
    shocks = np.zeros(len(idx))               # market-wide crashes, e.g. "Covid" and "tariffs"
    for start, days in [(len(idx) - 1660, 18), (len(idx) - 380, 6), (len(idx) - 900, 4)]:
        shocks[start:start + days] = -0.035
    names = ["AAPL Apple", "MSFT Microsoft", "NVDA Nvidia", "AMZN Amazon", "META Meta",
             "TSLA Tesla", "JPM JPMorgan", "XOM Exxon", "KO Coca-Cola", "PFE Pfizer",
             "AMD AMD", "NFLX Netflix", "DIS Disney", "BA Boeing", "INTC Intel",
             "SHEL.L Shell", "AZN.L AstraZeneca", "HSBA.L HSBC", "RR.L Rolls-Royce",
             "BP.L BP", "ULVR.L Unilever", "BARC.L Barclays", "LLOY.L Lloyds", "TSCO.L Tesco"]
    meta, data = {}, {}
    for i in range(160):
        base = names[i % len(names)].split(" ", 1)
        t = base[0] if i < len(names) else f"DEMO{i}"
        meta[t] = {"name": base[1] if i < len(names) else f"Demo Co {i}",
                   "index": ["ftse100"] if t.endswith(".L") else ["sp500"]}
        drift = rng.normal(0.0003, 0.0004)
        cyc = 0.004 * np.sin(np.arange(len(idx)) / rng.uniform(20, 120) + rng.uniform(0, 6))
        ret = drift + cyc + rng.normal(0, 0.015, len(idx)) + shocks * rng.uniform(0.6, 1.4)
        volm = rng.uniform(1e6, 5e6, len(idx)) * (1 + 4 * (shocks < 0))
        if i % 17 == 3:                      # a handful of stocks flushing right now
            ret[-7:-1] -= 0.045
            volm[-4:] *= 5
            ret[-1] = 0.05 if i % 2 else -0.01
        px = 100 * np.exp(np.cumsum(ret))
        hi = px * (1 + np.abs(rng.normal(0, 0.008, len(idx))))
        lo = px * (1 - np.abs(rng.normal(0, 0.008, len(idx))) - 0.03 * (shocks < 0))
        data[t] = pd.DataFrame({"Close": px, "High": hi, "Low": lo, "Volume": volm}, index=idx)
    return meta, data


def market_refs():
    """S&P 500 index and VIX, for context on the Washouts tab."""
    import yfinance as yf
    out = {}
    for sym, key in [("^GSPC", "spx"), ("^VIX", "vix")]:
        try:
            c = yf.download(sym, period=f"{HISTORY_YEARS}y", interval="1d", progress=False)["Close"].dropna()
            out[key] = c.iloc[:, 0] if isinstance(c, pd.DataFrame) else c
        except Exception as e:
            print(f"  ! could not get {sym}: {e}")
    return out


def demo_refs(data):
    closes = pd.DataFrame({t: d["Close"] for t, d in data.items()})
    spx = (closes / closes.iloc[0]).mean(axis=1) * 3000
    vix = (15 + 900 * spx.pct_change().rolling(10).std().fillna(0)).clip(10, 85)
    return {"spx": spx, "vix": vix}


def build_market(rsis, refs):
    """% of liquid stocks with daily RSI below 30, every day, plus past washout events."""
    if not rsis:
        return None
    frame = pd.DataFrame(rsis)
    valid = frame.notna().sum(axis=1)
    pct = (frame.lt(MARKET_OVERSOLD_RSI).sum(axis=1) / valid * 100)[valid >= max(20, 0.3 * frame.shape[1])]
    pct = pct.dropna().iloc[30:]          # skip RSI warm-up at the start of the data
    spx, vix = refs.get("spx"), refs.get("vix")

    # group days above the event line into separate events
    events, cur = [], None
    for day, v in pct.items():
        if v >= MARKET_EVENT:
            if cur and (day - cur["last"]).days <= 30:
                cur["last"] = day
                if v > cur["peak"]:
                    cur.update(peak=v, date=day)
            else:
                cur = {"date": day, "peak": v, "last": day}
                events.append(cur)
    ev_out = []
    for e in events:
        d = e["date"]
        row = {"date": d.strftime("%Y-%m-%d"), "peak": r(e["peak"], 1)}
        if spx is not None and d in spx.index:
            s = spx.loc[:d]
            row["spx_dd"] = r((s.iloc[-1] / s.iloc[-60:].max() - 1) * 100, 1)
            after = spx.loc[d:]
            for k, n in (("m1", 21), ("m3", 63), ("m6", 126)):
                row[k] = r((after.iloc[n] / after.iloc[0] - 1) * 100, 1) if len(after) > n else None
        if vix is not None:
            v = vix.loc[d - pd.Timedelta(days=7): d + pd.Timedelta(days=7)]
            row["vix"] = r(v.max(), 1) if len(v) else None
        ev_out.append(row)

    weekly = pct.resample("W-FRI").max().dropna()      # keeps every spike, small enough for the chart
    return {
        "now": r(pct.iloc[-1], 1), "date": pct.index[-1].strftime("%Y-%m-%d"),
        "vix": r(vix.iloc[-1], 1) if vix is not None and len(vix) else None,
        "stress": MARKET_STRESS, "event": MARKET_EVENT,
        "recent": [r(v, 1) for v in pct.tail(60)],
        "hist_dates": [d.strftime("%Y-%m-%d") for d in weekly.index],
        "hist": [r(v, 1) for v in weekly],
        "events": ev_out[::-1],
        "stocks": int(frame.shape[1]),
    }


def get_fx():
    """GBP/USD rate, used by the dashboard to show US profit/loss in pounds."""
    import yfinance as yf
    for attempt in range(3):
        try:
            c = yf.download("GBPUSD=X", period="10d", interval="1d", progress=False)["Close"].dropna()
            c = c.iloc[:, 0] if isinstance(c, pd.DataFrame) else c
            return {"GBPUSD": round(float(c.iloc[-1]), 5), "date": c.index[-1].strftime("%Y-%m-%d")}
        except Exception as e:
            print(f"  fx retry ({e})")
            time.sleep(3)
    print("  ! could not get GBP/USD rate")
    return None


def main():
    demo = "--demo" in sys.argv
    print("Loading ticker list...")
    if demo:
        meta, data = demo_data()
        fx = {"GBPUSD": 1.27, "date": datetime.now().strftime("%Y-%m-%d")}
    else:
        meta = load_universe()
        print(f"Total unique tickers: {len(meta)}")
        data = download(sorted(meta))
        for t, c in uk_currencies([t for t in data if t.endswith(".L")]).items():
            meta.setdefault(t, {})["ccy"] = c
        fx = get_fx()

    import crypto as cx
    cdata, snap, snap_day = {}, None, None
    if INCLUDE_CRYPTO:
        print("Loading crypto...")
        try:
            if demo:
                cmeta, cdata, snap = cx.demo_data()
                snap_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            else:
                cdata = cx.download()
                cmeta = {t: {"name": t[:-4], "index": ["crypto"]} for t in cdata}
                snap, snap_day = cx.derivatives()
            meta.update(cmeta)
            data.update(cdata)
        except Exception as e:
            print(f"  ! crypto skipped: {e}")

    print("Calculating RSI...")
    assets, rsis, crsis = {}, {}, {}
    for t, df in data.items():
        try:
            res = analyse(t, df, meta.get(t, {}))
            if res:
                assets[t], rsi_d = res
                if assets[t]["liquid"]:
                    (crsis if t.endswith("-USD") else rsis)[t] = rsi_d
        except Exception as e:
            print(f"  ! {t}: {e}")

    print("Measuring market breadth...")
    stock_data = {t: d for t, d in data.items() if not t.endswith("-USD")}
    refs = demo_refs(stock_data) if demo else market_refs()
    market = build_market(rsis, refs)
    crypto_market, leverage = None, None
    if cdata:
        liquid = {t: d for t, d in cdata.items() if t in crsis}
        crypto_market = cx.build_market(liquid, crsis, r)
        leverage = cx.attach_derivs(assets, snap, snap_day, ROOT / ("derivs_demo.json" if demo else "derivs_history.json"))
        if crypto_market:
            crypto_market["leverage"] = leverage

    counts = {tf: {s: 0 for s in ("TAP", "BOUNCE", "RECLAIM")} for tf in "DWM"}
    for a in assets.values():
        if not a["liquid"]:
            continue
        for tf, v in a["tf"].items():
            if v and v["signal"]:
                counts[tf][v["signal"]] += 1

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "demo": demo,
        "settings": {
            "rsi_period": RSI_PERIOD, "closed_candles_only": CLOSED_CANDLES_ONLY,
            "tap": [TAP_LOW, TAP_HIGH], "bounce": [BOUNCE_LOW, BOUNCE_HIGH],
            "reclaim_dip": RECLAIM_DIP, "lookback": LOOKBACK,
            "min_value": MIN_AVG_DAILY_VALUE, "universes": UNIVERSES,
            "wash": {"drop": WASH_DROP, "crypto_drop": cx.CRYPTO_WASH_DROP, "rsi": WASH_RSI, "vol": WASH_VOL, "window": WASH_WINDOW},
        },
        "counts": counts,
        "fx": fx,
        "market": market,
        "crypto_market": crypto_market,
        "assets": assets,
    }
    text = json.dumps(out, separators=(",", ":"))
    text = re.sub(r"\bNaN\b|-?\bInfinity\b", "null", text)   # browsers can't read NaN in JSON
    (ROOT / "data.json").write_text(text)
    print(f"Done: {len(assets)} assets scanned -> data.json (GBP/USD {fx and fx['GBPUSD']})")
    for tf in "DWM":
        print(f"  {tf}: {counts[tf]}")
    if market:
        print(f"  Market: {market['now']}% of stocks oversold, {len(market['events'])} past washout events")
    print(f"  Washing out now: {sum(1 for a in assets.values() if a['wash'])}")
    if crypto_market:
        print(f"  Crypto: median wick {crypto_market['now']}%, {len(crypto_market['events'])} past cascades, leverage {leverage}")


if __name__ == "__main__":
    main()
