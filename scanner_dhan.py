"""Dhan HQ v2 historical-data fetchers — drop-in replacement for
scanner.py's yfinance-based fetch_ohlc / fetch_ohlc_batch.

Why this exists
---------------
yfinance does NOT properly adjust Indian demergers/bonuses/spin-offs
(e.g., VEDL Apr-2026 demerger left a 720→271 phantom drop in yf data
even with auto_adjust=True). Dhan returns the same adjusted historical
series shown on the Dhan chart, so zone levels stay on one consistent
price scale across history.

Public API (matches scanner.py exactly)
---------------------------------------
    fetch_ohlc(symbol, timeframe)        -> pd.DataFrame | None
    fetch_ohlc_batch(symbols, timeframe) -> dict[symbol, pd.DataFrame]

Returned frames have a naive DatetimeIndex (name "Date") and the
canonical columns ["Open", "High", "Low", "Close", "Volume"] — exactly
what the yfinance fetcher returns, so detect_zones / compute_trend /
compute_ema20 / etc. keep working without changes.

Timeframe routing
-----------------
    "1d"            → Dhan /charts/historical (daily), paginated 90d chunks
    "1wk"/"1mo"/"3mo" → fetch daily, resample locally
    "125m"          → Dhan /charts/intraday at 25-min interval,
                       aggregate 5 consecutive 25m bars → 1 × 125m bar
    "5m"            → Dhan /charts/intraday at 5-min interval (raw)
    "25m"           → Dhan /charts/intraday at 25-min interval (raw)
    "60m"           → Dhan /charts/intraday at 60-min interval (raw)

Why 25-min and not 5-min for the 125m path
------------------------------------------
Dhan does NOT expose 125-min natively (intraday intervals are only
1, 5, 15, 25, 60). 25 divides 125 exactly (5 bars per group) and
the NSE session length (375 min) is divisible by both — alignment
is perfect with NO leftover bars per day. Fetching 25m delivers 5×
less bandwidth than fetching 5m for the same aggregated result.

Limits / behaviour
------------------
    * Dhan caps each request at 90 days (daily) or ~5 days (intraday).
      We paginate transparently.
    * Rate limit assumed at 5 req/sec → 0.21s sleep between requests.
    * Network / HTTP failures surface via Telegram (same `alert_once`
      dedup pattern as scanner.fetch_dhan_ltps).
    * Phantom (zero-volume / weekend) bars are filtered after fetch.
"""
from __future__ import annotations

import os
import time
from typing import Iterable

import pandas as pd
import requests

# Reuse scanner.py helpers so behaviour stays consistent.
from scanner import (
    DHAN_TOKEN, DHAN_CLIENT_ID,
    period_for, _drop_phantom_bars,
    alert_once, load_dhan_security_ids,
)


# ─── CONFIG ─────────────────────────────────────────────────────────────
DHAN_HIST_URL     = "https://api.dhan.co/v2/charts/historical"
DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"

# Per-request window limits (Dhan-imposed)
DHAN_HIST_CHUNK_DAYS     = 90    # max days per /charts/historical request
DHAN_INTRADAY_CHUNK_DAYS = 5     # ~5 trading days per /charts/intraday request

# Rate limit ≈ 5 req/sec → sleep 0.21s between calls (process-wide).
DHAN_RATE_SLEEP   = float(os.environ.get("DHAN_RATE_SLEEP",   "0.21"))
DHAN_HTTP_TIMEOUT = int(os.environ.get("DHAN_HTTP_TIMEOUT",   "20"))

# Defaults for NSE equities — overridable per call if needed later
DHAN_DEFAULT_SEGMENT    = "NSE_EQ"
DHAN_DEFAULT_INSTRUMENT = "EQUITY"

# ─── PERIOD MAPPING (yfinance string → integer days) ────────────────────
_PERIOD_DAYS = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "5y": 5 * 365,
    "10y": 10 * 365, "15y": 15 * 365, "20y": 20 * 365,
}


def _period_to_days(period_str: str) -> int:
    if period_str in _PERIOD_DAYS:
        return _PERIOD_DAYS[period_str]
    if period_str.endswith("d"):
        return int(period_str[:-1])
    if period_str.endswith("mo"):
        return int(period_str[:-2]) * 30
    if period_str.endswith("y"):
        return int(period_str[:-1]) * 365
    return 5 * 365   # safe default


# ─── PROCESS-WIDE THROTTLE ──────────────────────────────────────────────
_last_call_ts: float = 0.0


def _throttle() -> None:
    global _last_call_ts
    gap = time.monotonic() - _last_call_ts
    if gap < DHAN_RATE_SLEEP:
        time.sleep(DHAN_RATE_SLEEP - gap)
    _last_call_ts = time.monotonic()


# ─── ERROR ALERTS (mirror scanner.fetch_dhan_ltps pattern) ──────────────
def _alert_http_error(status_code: int, where: str) -> None:
    if status_code in (401, 403):
        alert_once(
            tag      = "dhan_hist_auth_fail",
            severity = "CRITICAL",
            title    = f"Dhan historical-data auth rejected (HTTP {status_code})",
            detail   = (f"Failed at {where}. Access token expired/revoked. "
                        "Run the refresh-token workflow."),
        )
    else:
        alert_once(
            tag      = f"dhan_hist_http_{status_code}",
            severity = "WARNING",
            title    = f"Dhan historical-data HTTP {status_code} at {where}",
            detail   = (f"Single-stock fetch failed at {where}. "
                        "Likely rate limit (429), upstream outage (5xx), "
                        "or request-shape change. Other stocks unaffected."),
        )


def _alert_exception(exc: Exception, where: str) -> None:
    alert_once(
        tag      = "dhan_hist_exception",
        severity = "WARNING",
        title    = f"Dhan historical-data exception ({type(exc).__name__})",
        detail   = (f"Failed at {where}: {type(exc).__name__}: {exc}. "
                    "Other stocks unaffected."),
    )


# ─── REQUEST + RESPONSE PARSE ──────────────────────────────────────────
# Max retries on HTTP 429 (rate limit). Exponential backoff: 2s, 4s, 8s.
DHAN_429_RETRIES = int(os.environ.get("DHAN_429_RETRIES", "3"))


def _request(url: str, body: dict, where: str) -> dict | None:
    """One POST to Dhan with auth, throttle, and error → alert pipeline.

    On HTTP 429, retries up to DHAN_429_RETRIES times with exponential
    backoff (2s, 4s, 8s). Only surfaces a Telegram alert if all retries
    fail — momentary bursts shouldn't generate noise.
    """
    if not DHAN_TOKEN or not DHAN_CLIENT_ID:
        return None
    headers = {
        "access-token":  DHAN_TOKEN,
        "client-id":     DHAN_CLIENT_ID,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    backoff = 2.0
    for attempt in range(DHAN_429_RETRIES + 1):
        _throttle()
        try:
            r = requests.post(url, json=body, headers=headers, timeout=DHAN_HTTP_TIMEOUT)
        except Exception as e:
            _alert_exception(e, where)
            return None
        if r.ok:
            try:
                return r.json()
            except ValueError as e:
                _alert_exception(e, where)
                return None
        if r.status_code == 429 and attempt < DHAN_429_RETRIES:
            print(f"  Dhan {where}: HTTP 429 — sleeping {backoff:.1f}s then retry "
                  f"({attempt + 1}/{DHAN_429_RETRIES})")
            time.sleep(backoff)
            backoff *= 2
            continue
        # Final failure (non-retryable or out of retries)
        print(f"  Dhan {where}: HTTP {r.status_code} (body redacted)")
        _alert_http_error(r.status_code, where)
        return None
    return None


def _response_to_df(resp: dict | None) -> pd.DataFrame:
    """Convert {open:[…], high:[…], low:[…], close:[…], volume:[…],
    timestamp:[…]} → DataFrame indexed by IST timestamp."""
    if not resp:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    ts = resp.get("timestamp") or []
    if not ts:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
    df = pd.DataFrame({
        "Open":   resp.get("open",   []) or [],
        "High":   resp.get("high",   []) or [],
        "Low":    resp.get("low",    []) or [],
        "Close":  resp.get("close",  []) or [],
        "Volume": resp.get("volume", []) or [],
    }, index=idx)
    df.index.name = "Date"
    return df


# ─── DAILY-DATA CACHE ───────────────────────────────────────────────────
# Per-process cache so a stock's daily series is fetched only ONCE even
# when the caller asks for 1d (1y), 1wk (5y), 1mo (10y), 3mo (15y) — all
# four resolve to a single longest fetch, with shorter requests sliced
# from the cache.
#   key   = secid (int)
#   value = (covered_days, dataframe)  — covered_days is the request span
_daily_cache: dict[int, tuple[int, pd.DataFrame]] = {}


# ─── PAGINATED FETCHERS ─────────────────────────────────────────────────
def _fetch_daily_paginated(secid: int, total_days: int) -> pd.DataFrame:
    """Fetch up to `total_days` of daily OHLC in DHAN_HIST_CHUNK_DAYS chunks.

    Cached: if we've previously fetched as many or more days for this secid,
    return a slice of the cached frame instead of re-fetching.
    """
    today        = pd.Timestamp.now(tz="Asia/Kolkata").normalize()
    start_target = today - pd.Timedelta(days=total_days)

    cached = _daily_cache.get(secid)
    if cached is not None:
        covered_days, cached_df = cached
        if covered_days >= total_days and not cached_df.empty:
            cutoff = pd.Timestamp(start_target).tz_localize(None) \
                     if cached_df.index.tz is None \
                     else start_target
            return cached_df[cached_df.index >= cutoff]

    pieces: list[pd.DataFrame] = []
    cursor_end = today
    while cursor_end > start_target:
        cursor_start = max(cursor_end - pd.Timedelta(days=DHAN_HIST_CHUNK_DAYS),
                           start_target)
        body = {
            "securityId":      str(secid),
            "exchangeSegment": DHAN_DEFAULT_SEGMENT,
            "instrument":      DHAN_DEFAULT_INSTRUMENT,
            "expiryCode":      0,
            "oi":              False,
            "fromDate":        cursor_start.strftime("%Y-%m-%d"),
            "toDate":          cursor_end.strftime("%Y-%m-%d"),
        }
        where = f"daily(sid={secid} {body['fromDate']}..{body['toDate']})"
        resp = _request(DHAN_HIST_URL, body, where)
        df   = _response_to_df(resp)
        if not df.empty:
            pieces.append(df)
        if cursor_start <= start_target:
            break
        cursor_end = cursor_start

    if not pieces:
        return pd.DataFrame()
    combined = pd.concat(pieces).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    # Store with tz stripped — downstream code expects naive daily timestamps
    if getattr(combined.index, "tz", None) is not None:
        combined.index = combined.index.tz_convert("Asia/Kolkata").tz_localize(None)
    _daily_cache[secid] = (total_days, combined)
    return combined


def _fetch_intraday_paginated(secid: int, interval: str,
                              total_days: int) -> pd.DataFrame:
    """Fetch up to `total_days` of intraday OHLC at the given Dhan interval
    (1/5/15/25/60), paginated in DHAN_INTRADAY_CHUNK_DAYS chunks."""
    today        = pd.Timestamp.now(tz="Asia/Kolkata").normalize()
    start_target = today - pd.Timedelta(days=total_days)
    pieces: list[pd.DataFrame] = []

    cursor_end = today
    while cursor_end > start_target:
        cursor_start = max(cursor_end - pd.Timedelta(days=DHAN_INTRADAY_CHUNK_DAYS),
                           start_target)
        body = {
            "securityId":      str(secid),
            "exchangeSegment": DHAN_DEFAULT_SEGMENT,
            "instrument":      DHAN_DEFAULT_INSTRUMENT,
            "interval":        interval,
            "oi":              False,
            "fromDate":        cursor_start.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate":          cursor_end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        where = f"intra-{interval}m(sid={secid} {body['fromDate']}..{body['toDate']})"
        resp  = _request(DHAN_INTRADAY_URL, body, where)
        df    = _response_to_df(resp)
        if not df.empty:
            pieces.append(df)
        if cursor_start <= start_target:
            break
        cursor_end = cursor_start

    if not pieces:
        return pd.DataFrame()
    combined = pd.concat(pieces).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    return combined


# ─── 25-MIN → 125-MIN AGGREGATOR (NSE-session-aligned) ──────────────────
def aggregate_25m_to_125m(df_25m: pd.DataFrame) -> pd.DataFrame:
    """5 consecutive 25-min bars → 1 × 125-min bar, NSE-session-aligned.

    NSE session = 9:15-15:30 IST = 375 min = exactly 3 × 125-min bars per day:
        Bar 1:  9:15 → 11:20  (25m bars at 9:15, 9:40, 10:05, 10:30, 10:55)
        Bar 2: 11:20 → 13:25  (25m bars at 11:20, 11:45, 12:10, 12:35, 13:00)
        Bar 3: 13:25 → 15:30  (25m bars at 13:25, 13:50, 14:15, 14:40, 15:05)

    Bucket by (date, minutes-since-9:15) // 125.
    """
    if df_25m is None or df_25m.empty:
        return pd.DataFrame()

    idx = df_25m.index
    if getattr(idx, "tz", None) is None:
        try:
            df_25m = df_25m.tz_localize("Asia/Kolkata")
            idx = df_25m.index
        except Exception:
            pass
    elif str(idx.tz) != "Asia/Kolkata":
        try:
            df_25m = df_25m.tz_convert("Asia/Kolkata")
            idx = df_25m.index
        except Exception:
            pass

    session_start_min = 9 * 60 + 15
    minute_of_day     = idx.hour * 60 + idx.minute
    minutes_from_open = minute_of_day - session_start_min
    bucket = (minutes_from_open // 125).astype("int64")    # 0, 1, or 2

    date_key = idx.tz_convert("Asia/Kolkata").date if hasattr(idx, "tz_convert") else idx.date
    keys = pd.MultiIndex.from_arrays(
        [pd.Index([d for d in date_key]), bucket],
        names=["date", "bucket"],
    )
    grouped = df_25m.groupby(keys)
    agg = grouped.agg(
        Open   = ("Open",   "first"),
        High   = ("High",   "max"),
        Low    = ("Low",    "min"),
        Close  = ("Close",  "last"),
        Volume = ("Volume", "sum"),
    ).dropna(subset=["Open", "Close"])

    # Build a timestamp index at the BAR START (9:15, 11:20, or 13:25).
    bar_start_min = {0: 9 * 60 + 15, 1: 11 * 60 + 20, 2: 13 * 60 + 25}
    timestamps = []
    for date, bkt in agg.index:
        m = bar_start_min[int(bkt)]
        timestamps.append(pd.Timestamp(date).replace(hour=m // 60, minute=m % 60))
    agg.index = pd.DatetimeIndex(timestamps, name="Date").tz_localize("Asia/Kolkata")
    return agg.sort_index()


# ─── DAILY → COARSER AGGREGATION ────────────────────────────────────────
_RESAMPLE_RULE = {"1wk": "W-MON", "1mo": "ME", "3mo": "QE"}


def _resample_daily(df_daily: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample daily bars to weekly/monthly/quarterly with O/H/L/C/V rules."""
    rule = _RESAMPLE_RULE[tf]
    if df_daily is None or df_daily.empty:
        return pd.DataFrame()
    df = df_daily.copy()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    agg = df.resample(rule, label="left", closed="left").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(subset=["Open", "Close"])
    return agg


# ─── PUBLIC API ─────────────────────────────────────────────────────────
def fetch_ohlc(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Drop-in replacement for scanner.fetch_ohlc, backed by Dhan v2.

    Returns None if:
      * Symbol not in dhan_security_ids.json
      * Token/client-id not configured
      * Dhan returned no data
      * Fewer than 20 bars after filtering
    """
    secid_map = load_dhan_security_ids()
    if symbol not in secid_map:
        return None
    secid = secid_map[symbol]
    days  = _period_to_days(period_for(timeframe))

    if timeframe in ("1wk", "1mo", "3mo"):
        df_daily = _fetch_daily_paginated(secid, days)
        if df_daily.empty:
            return None
        df = _resample_daily(df_daily, timeframe)
    elif timeframe == "1d":
        df = _fetch_daily_paginated(secid, days)
        if df.empty:
            return None
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    elif timeframe == "125m":
        df_25m = _fetch_intraday_paginated(secid, "25", days)
        if df_25m.empty:
            return None
        df = aggregate_25m_to_125m(df_25m)
    elif timeframe in ("5m", "25m", "60m"):
        df = _fetch_intraday_paginated(secid, timeframe[:-1], days)
        if df.empty:
            return None
    else:
        return None

    df = _drop_phantom_bars(df)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return df if len(df) >= 20 else None


def fetch_ohlc_batch(symbols: Iterable[str], timeframe: str,
                     chunk_size: int = 1) -> dict[str, pd.DataFrame]:
    """Sequential per-symbol fetch.

    Dhan historical endpoints accept only one securityId per request, so
    no real batching is possible. The `chunk_size` parameter is accepted
    for signature compatibility with scanner.fetch_ohlc_batch and ignored.
    Rate limiting is enforced by the process-wide throttle.
    """
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        df = fetch_ohlc(s, timeframe)
        if df is not None:
            out[s] = df
    return out


# ─── CLI: smoke-test ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if not (DHAN_TOKEN and DHAN_CLIENT_ID):
        print("Missing DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID env vars — abort.")
        sys.exit(1)

    sym = sys.argv[1] if len(sys.argv) > 1 else "VEDL"
    tf  = sys.argv[2] if len(sys.argv) > 2 else "1wk"
    print(f"Fetching {sym} {tf} from Dhan…")
    df = fetch_ohlc(sym, tf)
    if df is None or df.empty:
        print("  FAILED — no data returned.")
        sys.exit(2)
    print(f"  rows: {len(df)}  range: {df.index[0]} → {df.index[-1]}")
    print("  Last 10 bars:")
    print(df.tail(10).to_string())
