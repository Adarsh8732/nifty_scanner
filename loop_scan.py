"""Long-running scanner loop — one 6-hour session per trading day.

Triggered once by cron-job.org at 9:14 AM IST. Runs until 3:15 PM IST
(within GitHub Actions' 6-hour single-job limit). Inside the loop:
  * In-memory yfinance cache, refreshed hourly (not on every iteration)
  * Dhan LTPs fetched every iteration (~1 sec)
  * Zone detection + entry-line crossing checks per stock per timeframe
  * Telegram alert on fresh crossings, deduped via in-memory state
  * State persisted to repo at the end of the session

Imports all heavy lifting from scanner.py — this file just orchestrates.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from scanner import (
    # Detection + filters
    detect_zones, compute_trend, htf_status, passes_strict_filter,
    passes_125m_strict_filter,
    is_approaching, zone_key, build_alert_msg,
    # Fetching
    fetch_ohlc_batch, fetch_ohlc, fetch_dhan_ltps, load_dhan_security_ids,
    # IO
    send_telegram,
    # LLM (Gemini)
    analyze_with_gemini, USE_LLM,
    # Helpers
    state_path, trend_tf_for, zone_tf_for, tf_label,
    # Config (already populated from env in scanner.py module load)
    TIMEFRAMES, ALERT_MIN_SCORE, STRICT_FILTER,
)
from symbols import ALL_SYMBOLS

IST                  = timezone(timedelta(hours=5, minutes=30))
POLL_SECONDS         = int(os.environ.get("POLL_SECONDS",   "30"))   # how often to fetch Dhan LTPs
CACHE_REFRESH_MINS   = int(os.environ.get("CACHE_REFRESH_MINS", "60"))  # refresh yfinance every N min
# Session naturally ends when GitHub Actions kills the job at its
# timeout-minutes limit (360 min = 6 h). The SIGTERM handler below
# catches the kill signal and saves state cleanly before exit.


def now_ist() -> datetime:
    return datetime.now(IST)


def patch_live_close(df: pd.DataFrame, ltp: float) -> pd.DataFrame:
    """Replace today's bar's Close with live LTP, expand High/Low if needed.
    Returns a SHALLOW copy with only the last row mutated."""
    df = df.copy(deep=False)
    df.iloc[-1, df.columns.get_loc("Close")] = ltp
    df.iloc[-1, df.columns.get_loc("High")]  = max(df["High"].iloc[-1], ltp)
    df.iloc[-1, df.columns.get_loc("Low")]   = min(df["Low"].iloc[-1], ltp)
    return df


def fetch_caches(symbols: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    """Fetch fresh OHLC for all timeframes. Returns {tf: {sym: df}}."""
    caches = {}
    for tf in TIMEFRAMES:
        t0 = time.time()
        caches[tf] = fetch_ohlc_batch(symbols, tf)
        print(f"  cache[{tf}]: {len(caches[tf])} symbols in {time.time()-t0:.1f}s")
    return caches


def load_states() -> dict[str, dict]:
    """Load previously-alerted state per timeframe (carry across days)."""
    out = {}
    for tf in TIMEFRAMES:
        p = state_path(tf)
        out[tf] = json.loads(p.read_text()) if p.exists() else {}
    return out


def save_states(state: dict[str, dict]) -> None:
    """Persist state to disk. Caller (workflow) commits to repo."""
    for tf, s in state.items():
        state_path(tf).write_text(json.dumps(s, indent=2, sort_keys=True))


# ─── HTF fetch with mini-cache to avoid repeat calls within same iteration ──
class HtfCache:
    """Tiny dict-based cache of HTF DataFrames + zones. TTL = session lifetime
    (cleared per cache-refresh tick along with main cache)."""

    def __init__(self):
        self._trend: dict[tuple, int]            = {}
        self._zones: dict[tuple, dict]           = {}

    def get_trend(self, sym: str, tf: str) -> int:
        key = (sym, tf)
        if key not in self._trend:
            df = fetch_ohlc(sym, tf)
            self._trend[key] = compute_trend(df) if df is not None else 0
        return self._trend[key]

    def get_zones(self, sym: str, tf: str) -> dict:
        key = (sym, tf)
        if key not in self._zones:
            df = fetch_ohlc(sym, tf)
            self._zones[key] = detect_zones(df) if df is not None else {"demand": None, "supply": None}
        return self._zones[key]

    def clear(self):
        self._trend.clear()
        self._zones.clear()


def scan_iteration(symbols, caches, live_ltps, state, htf_cache):
    """One pass over all symbols × timeframes. Returns list of (sym, msg) alerts to send."""
    alerts: list[tuple[str, str]] = []

    for tf in TIMEFRAMES:
        cache = caches.get(tf, {})
        for sym in symbols:
            df = cache.get(sym)
            if df is None:
                continue

            ltp = live_ltps.get(sym)
            if ltp is not None and ltp > 0:
                df = patch_live_close(df, ltp)

            close_now = float(df["Close"].iloc[-1])
            zones = detect_zones(df)
            dem, sup = zones["demand"], zones["supply"]

            for z in (dem, sup):
                if z is None or z["score"] < ALERT_MIN_SCORE:
                    continue
                if not is_approaching(close_now, z):
                    continue
                key = zone_key(sym, z)
                if key in state[tf]:
                    continue  # already alerted in this or prior session

                # Strict filter — fetch HTF lazily through the cache
                if STRICT_FILTER:
                    trend_htf = htf_cache.get_trend(sym, trend_tf_for(tf))
                    htf_z     = htf_cache.get_zones(sym, zone_tf_for(tf))

                    if tf == "125m":
                        # Custom 30%-closeness rule + W zone score >= 7
                        if not passes_125m_strict_filter(
                            z["type"], z, trend_htf,
                            htf_z["demand"], htf_z["supply"],
                            w_score_threshold=ALERT_MIN_SCORE,
                        ):
                            continue
                    else:
                        # Existing rule for 1d/1wk: closer HTF + matching trend
                        if not passes_strict_filter(z["type"], trend_htf,
                                                    htf_z["demand"], htf_z["supply"]):
                            continue
                else:
                    trend_htf = 0
                    htf_z     = {"demand": None, "supply": None}

                ltf_trend = compute_trend(df)
                msg = build_alert_msg(sym, z, close_now, tf,
                                      ltf_trend, trend_htf,
                                      htf_z["demand"], htf_z["supply"])

                # LLM enrichment (Gemini). No-op if USE_LLM=false.
                if USE_LLM:
                    analysis = analyze_with_gemini(
                        sym, z, close_now, tf,
                        ltf_trend, trend_htf,
                        htf_z["demand"], htf_z["supply"],
                    )
                    if analysis:
                        msg = msg + "\n─────────\n*🧠 AI thesis:*\n" + analysis

                alerts.append((sym, msg))
                state[tf][key] = {
                    "first_alerted": now_ist().isoformat(),
                    "cmp_at_alert":  close_now,
                    "score":         z["score"],
                }
    return alerts


# ─── Graceful shutdown on SIGTERM (GitHub kills jobs near limit) ──────────
class GracefulExit:
    def __init__(self):
        self.requested = False
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT,  self._handler)

    def _handler(self, signum, frame):
        print(f"\n[{now_ist()}] Received signal {signum} — finishing iteration and exiting cleanly")
        self.requested = True


def main() -> int:
    print(f"━━━ Loop Scan starting at {now_ist().strftime('%H:%M:%S IST')} ━━━")
    print(f"Symbols:        {len(ALL_SYMBOLS)}")
    print(f"Timeframes:     {', '.join(TIMEFRAMES)}")
    print(f"Poll cadence:   {POLL_SECONDS}s")
    print(f"Cache refresh:  every {CACHE_REFRESH_MINS} min")
    print(f"Session ends:   when GitHub Actions kills the job at its 6h limit")

    # Initial setup
    print(f"\n[{now_ist()}] Loading Dhan security IDs + initial caches...")
    secid_map = load_dhan_security_ids()
    if not secid_map:
        print("  WARNING: no Dhan security map — LTPs will fall back to yfinance close")

    caches = fetch_caches(ALL_SYMBOLS)
    last_cache_refresh = time.time()

    state = load_states()
    state_counts_at_start = {tf: len(state[tf]) for tf in TIMEFRAMES}
    print(f"State carried in: {state_counts_at_start}")

    htf_cache = HtfCache()
    graceful  = GracefulExit()
    total_alerts_sent = 0
    iteration = 0

    while not graceful.requested:
        iteration += 1
        loop_start = time.time()

        # Hourly: refresh yfinance + clear HTF cache
        if (time.time() - last_cache_refresh) > CACHE_REFRESH_MINS * 60:
            print(f"\n[{now_ist()}] Hourly cache refresh...")
            caches = fetch_caches(ALL_SYMBOLS)
            htf_cache.clear()
            last_cache_refresh = time.time()

        # Live LTPs
        live_ltps = fetch_dhan_ltps(ALL_SYMBOLS, secid_map)

        # Detect crossings + collect alerts
        alerts = scan_iteration(ALL_SYMBOLS, caches, live_ltps, state, htf_cache)

        # Send Telegram alerts
        for sym, msg in alerts:
            send_telegram(msg)
            total_alerts_sent += 1
            time.sleep(0.4)  # respect TG rate limit

        loop_time = time.time() - loop_start
        print(f"[{now_ist().strftime('%H:%M:%S')}] iter {iteration:4d} "
              f"| LTPs {len(live_ltps):3d} | alerts {len(alerts):2d} "
              f"| {loop_time:5.1f}s | total alerts {total_alerts_sent}")

        # Sleep, accounting for processing time. Wake early if exit requested.
        sleep_left = max(0.0, POLL_SECONDS - loop_time)
        while sleep_left > 0 and not graceful.requested:
            chunk = min(1.0, sleep_left)
            time.sleep(chunk)
            sleep_left -= chunk

    print(f"\n[{now_ist()}] Session ended. Saving state...")
    save_states(state)
    print(f"Total alerts sent: {total_alerts_sent}")
    print(f"Total iterations:  {iteration}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
