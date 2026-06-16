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
    build_chart_image, send_telegram_photo, calc_trade_levels,
    TG_CAPTION_MAX,
    # EMA20 confluence
    compute_ema20, EMA20_TFS,
    # Swing origin (W/M/3M)
    find_swing_origin, find_origin_htf_match,
    # Fetching
    fetch_ohlc_batch, fetch_ohlc, fetch_dhan_ltps, load_dhan_security_ids,
    # IO
    send_telegram, alert_once,
    # LLM (Gemini)
    analyze_with_gemini, USE_LLM,
    # Helpers
    state_path, trend_tf_for, zone_tf_for, tf_label, entry_pct_for,
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
    """Cache HTF DataFrames + trend; recompute zones per call with live LTP.

    Why cache the df but NOT the zones: detect_zones() depends on close_now
    (for dist_pct, best-zone selection, and the in-zone filter), and we now
    pass the live Dhan LTP as close_now_override on every call. Same HTF df
    + different LTP → different "best" zone, so a static zone cache would
    return stale results.

    The df itself doesn't change between cache refreshes, so caching it
    avoids the expensive yfinance call.
    """

    def __init__(self):
        self._trend: dict[tuple, int]                       = {}
        self._df:    dict[tuple, "pd.DataFrame | None"]    = {}
        self._ema20: dict[tuple, "float | None"]           = {}

    def _get_df(self, sym: str, tf: str):
        key = (sym, tf)
        if key not in self._df:
            self._df[key] = fetch_ohlc(sym, tf)
        return self._df[key]

    def get_trend(self, sym: str, tf: str) -> int:
        key = (sym, tf)
        if key not in self._trend:
            df = self._get_df(sym, tf)
            self._trend[key] = compute_trend(df) if df is not None else 0
        return self._trend[key]

    def get_zones(self, sym: str, tf: str,
                  close_now_override: float | None = None) -> dict:
        df = self._get_df(sym, tf)
        if df is None:
            return {"demand": None, "supply": None}
        return detect_zones(df, close_now_override=close_now_override)

    def get_ema20(self, sym: str, tf: str,
                  prefer_df: "pd.DataFrame | None" = None) -> float | None:
        """EMA20 for symbol+tf. If prefer_df is provided (already-fetched main
        batch cache), uses it directly without a separate yfinance call."""
        key = (sym, tf)
        if key in self._ema20:
            return self._ema20[key]
        df = prefer_df if prefer_df is not None else self._get_df(sym, tf)
        val = compute_ema20(df) if df is not None else None
        self._ema20[key] = val
        return val

    def clear(self):
        self._trend.clear()
        self._df.clear()
        self._ema20.clear()


def scan_iteration(symbols, caches, live_ltps, state, htf_cache):
    """One pass over all symbols × timeframes. Returns list of (sym, msg) alerts to send."""
    alerts: list[tuple[str, str]] = []

    for tf in TIMEFRAMES:
        cache = caches.get(tf, {})
        for sym in symbols:
            df = cache.get(sym)
            if df is None:
                continue

            # Current-price reference (single source of truth for this symbol).
            # Priority: live Dhan LTP > last CLOSED bar's close.
            # We never use df["Close"].iloc[-1] directly — that's the in-progress
            # bar (partial close) and contaminates zone detection (see Leak 1/2).
            ltp = live_ltps.get(sym)
            if ltp is not None and ltp > 0:
                close_now = float(ltp)
            elif len(df) > 1:
                close_now = float(df["Close"].iloc[-2])   # last closed bar
            else:
                close_now = float(df["Close"].iloc[-1])   # only-1-bar edge case

            # Detect zones on this df. IGNORE_INPROGRESS_BAR is on by default
            # inside detect_zones, so the in-progress bar is excluded from
            # legout/test-walk regardless of what we pass here.
            #
            # All LTF detection (D / W / 125m) uses the stricter REVERSAL
            # rule: legout close must close beyond legin close (instead of
            # the standard body-ratio check). HTF/MTF detection keeps the
            # standard ratio rule (flag defaults False in HtfCache call).
            zones = detect_zones(
                df,
                close_now_override=close_now,
                use_close_beyond_legin=True,
                entry_pct=entry_pct_for(tf),
            )
            dem, sup = zones["demand"], zones["supply"]

            for z in (dem, sup):
                if z is None or z["score"] < ALERT_MIN_SCORE:
                    continue
                if not is_approaching(close_now, z, tf):
                    continue
                key = zone_key(sym, z)
                if key in state[tf]:
                    continue  # already alerted in this or prior session

                # Strict filter — fetch HTF lazily through the cache.
                # Pass close_now so HTF zones are evaluated against the live
                # LTP (best HTF zone + dist_pct displayed in alert).
                if STRICT_FILTER:
                    trend_htf = htf_cache.get_trend(sym, trend_tf_for(tf))
                    htf_z     = htf_cache.get_zones(sym, zone_tf_for(tf),
                                                    close_now_override=close_now)

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

                # EMA20 confluence: D / W / M / 3M. Re-use main-cache dfs
                # when available (cheaper than HtfCache's per-symbol fetch).
                ema20s = {}
                for ema_tf in EMA20_TFS:
                    prefer = caches.get(ema_tf, {}).get(sym)
                    ema20s[ema_tf] = htf_cache.get_ema20(sym, ema_tf, prefer_df=prefer)

                # Swing origin: where did price come from before this alert?
                # Find recent peak (demand) or trough (supply) on the LTF,
                # then check if it sits inside a W/M/3M opposite-type zone.
                origin_price = find_swing_origin(df, z["type"])
                origin_htf_zones = {
                    "1wk": htf_cache.get_zones(sym, "1wk", close_now_override=close_now),
                    "1mo": htf_cache.get_zones(sym, "1mo", close_now_override=close_now),
                    "3mo": htf_cache.get_zones(sym, "3mo", close_now_override=close_now),
                }
                origin_match = find_origin_htf_match(
                    origin_price, z["type"], origin_htf_zones, ltf_timeframe=tf,
                )

                msg_short = build_alert_msg(sym, z, close_now, tf,
                                      ltf_trend, trend_htf,
                                      htf_z["demand"], htf_z["supply"],
                                      ema20s=ema20s,
                                      origin_price=origin_price,
                                      origin_match=origin_match)
                msg_full = msg_short

                # LLM enrichment (Gemini). No-op if USE_LLM=false.
                # Pass EMA20 confluence + swing origin so the model can use
                # the same structural signals shown in the Telegram alert.
                if USE_LLM:
                    analysis = analyze_with_gemini(
                        sym, z, close_now, tf,
                        ltf_trend, trend_htf,
                        htf_z["demand"], htf_z["supply"],
                        ema20s=ema20s,
                        origin_price=origin_price,
                        origin_match=origin_match,
                    )
                    if analysis:
                        msg_full = msg_short + "\n─────────\n*🧠 AI thesis:*\n" + analysis

                # Build the chart snapshot for this alert (non-fatal if fails)
                chart_bytes = build_chart_image(
                    sym, df, z, tf, levels=calc_trade_levels(z),
                )

                alerts.append({
                    "sym":         sym,
                    "msg_full":    msg_full,    # alert + LLM
                    "msg_short":   msg_short,   # alert only (no LLM)
                    "chart_bytes": chart_bytes, # PNG bytes or None
                })
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

        # Send Telegram alerts.
        # Strategy per alert:
        #   1. If chart rendered AND full message (alert + LLM) ≤ 1024 chars
        #      → send chart with full caption (one Telegram message).
        #   2. If chart rendered BUT full message > 1024 chars
        #      → send chart with msg_short (alert only, no LLM). LLM is
        #        sacrificed to keep the chart attached.
        #   3. If chart failed to render (mplfinance missing, error, etc.)
        #      → fall back to text-only send_telegram with full message.
        for a in alerts:
            sent_with_chart = False
            if a["chart_bytes"]:
                caption = a["msg_full"] if len(a["msg_full"]) <= TG_CAPTION_MAX \
                                        else a["msg_short"]
                sent_with_chart = send_telegram_photo(a["chart_bytes"], caption)
            if not sent_with_chart:
                # Fall back to text-only (full message — 4096 char limit applies)
                send_telegram(a["msg_full"])
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
    # Top-level guard: any uncaught exception in main() gets surfaced to
    # Telegram BEFORE the process exits, so a code bug or rare runtime
    # error doesn't fail silently in CI logs.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            alert_once(
                tag      = "scanner_crash",
                severity = "CRITICAL",
                title    = f"Scanner crashed: {type(e).__name__}",
                detail   = f"{e}\n\n{tb}",
            )
        except Exception:
            pass  # If TG itself is down, we tried — exit anyway
        sys.exit(1)
