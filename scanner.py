"""Nifty Zone Scanner — sends Telegram alerts on zone approach.

Logic mirrors scanner.pine f_scanZones():
- Walk back through OHLC bars to find legout → base → legin patterns
- Compute proximal/distal, score (freshness + strength + time-at-base, max 7)
- Alert when current price crosses the entry line (configurable % from proximal)
- State-based dedupe prevents repeat alerts while price stays inside

Configurable via env vars (see CONFIG section below).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from symbols import ALL_SYMBOLS

# ─── CONFIG (env vars override defaults) ────────────────────────────────
TG_TOKEN          = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "")
# Comma-separated list of yfinance intervals to scan: "1d,1wk"
TIMEFRAMES        = [tf.strip() for tf in os.environ.get("TIMEFRAMES", "1d,1wk").split(",") if tf.strip()]
ALERT_ENTRY_PCT   = float(os.environ.get("ALERT_ENTRY_PCT", "1.0"))
ALERT_MIN_SCORE   = float(os.environ.get("ALERT_MIN_SCORE", "7.0"))
SCAN_DEMAND       = os.environ.get("SCAN_DEMAND", "true").lower() == "true"
SCAN_SUPPLY       = os.environ.get("SCAN_SUPPLY", "true").lower() == "true"

# State file per timeframe to keep dedupe separate (daily ≠ weekly zones)
def state_path(tf: str) -> Path:
    return Path(f"state_{tf}.json")

def period_for(tf: str) -> str:
    return "1y" if tf == "1d" else "5y"

# Detection params (match Pine defaults)
BASE_PCT         = 0.50
EXCITE_PCT       = 0.50
LEGOUT_MIN_RATIO = 0.8
MAX_BASE         = 3
LOOKBACK_BARS    = 50
MAX_ZONE_TESTS   = 1


# ─── HELPERS ────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    """Push a markdown message to your Telegram chat."""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("  [no TG creds — would send]:", text.split('\n')[0])
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.ok:
            print(f"  TG error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  TG exception: {e}")


def fetch_ohlc(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch OHLC for an NSE symbol at the given timeframe. None on failure."""
    yf_sym = symbol + ".NS"
    try:
        df = yf.download(
            yf_sym, period=period_for(timeframe), interval=timeframe,
            progress=False, auto_adjust=False, threads=False,
        )
        if df is None or df.empty:
            return None
        # Flatten multi-level columns if yfinance returned them
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].dropna()
        return df if len(df) >= 20 else None
    except Exception as e:
        print(f"  fetch error: {e}")
        return None


# ─── ZONE DETECTION (Pine port) ─────────────────────────────────────────
def detect_zones(df: pd.DataFrame) -> dict:
    """Detect best (closest to current price) demand and supply zones.

    Mirrors scanner.pine f_scanZones(). Returns:
        {"demand": {...} | None, "supply": {...} | None}
    where each zone dict has: proximal, distal, score, tests, dist_pct, type.
    """
    # Reverse so index 0 = latest bar (matches Pine's [N] indexing)
    df_rev = df.iloc[::-1].reset_index(drop=True)
    O = df_rev["Open"].values
    H = df_rev["High"].values
    L = df_rev["Low"].values
    C = df_rev["Close"].values
    n = len(df_rev)

    close_now = float(C[0])
    best_dem = None
    best_sup = None

    max_start = min(LOOKBACK_BARS, n - MAX_BASE - 3)
    for start_bar in range(1, max_start + 1):
        lo_o, lo_h, lo_l, lo_c = O[start_bar], H[start_bar], L[start_bar], C[start_bar]
        lo_body = abs(lo_c - lo_o)
        lo_rng  = lo_h - lo_l
        lo_bpct = lo_body / lo_rng if lo_rng > 0 else 0.0
        lo_grn  = lo_c > lo_o
        lo_red  = lo_c < lo_o

        if lo_body == 0 or lo_bpct < EXCITE_PCT or not (lo_grn or lo_red):
            continue

        # Walk back through base candles
        base_cnt = 0
        ii = start_bar + 1
        b_hb = b_lb = b_hw = b_lw = None
        while base_cnt < MAX_BASE and ii < n - 1:
            c_o, c_h, c_l, c_c = O[ii], H[ii], L[ii], C[ii]
            c_rng  = c_h - c_l
            c_bpct = abs(c_c - c_o) / c_rng if c_rng > 0 else 0.0
            if c_bpct < BASE_PCT:
                bh = max(c_o, c_c); bl = min(c_o, c_c)
                b_hb = bh if b_hb is None else max(b_hb, bh)
                b_lb = bl if b_lb is None else min(b_lb, bl)
                b_hw = c_h if b_hw is None else max(b_hw, c_h)
                b_lw = c_l if b_lw is None else min(b_lw, c_l)
                base_cnt += 1
                ii += 1
            else:
                break

        if base_cnt < 1 or ii >= n:
            continue

        # Legin candle
        leg_o, leg_h, leg_l, leg_c = O[ii], H[ii], L[ii], C[ii]
        leg_body = abs(leg_c - leg_o)
        leg_rng  = leg_h - leg_l
        leg_bpct = leg_body / leg_rng if leg_rng > 0 else 0.0
        if leg_body == 0 or leg_bpct < EXCITE_PCT:
            continue

        legin_red = leg_c < leg_o
        legin_grn = leg_c > leg_o
        is_reversal = (lo_grn and legin_red) or (lo_red and legin_grn)
        if is_reversal and lo_body < LEGOUT_MIN_RATIO * leg_body:
            continue

        # Strength score helper (next-candle exciting check)
        if start_bar >= 1:
            nx_o, nx_h, nx_l, nx_c = O[start_bar - 1], H[start_bar - 1], L[start_bar - 1], C[start_bar - 1]
            nx_rng  = nx_h - nx_l
            nx_bpct = abs(nx_c - nx_o) / nx_rng if nx_rng > 0 else 0.0
        else:
            nx_bpct = 0.0

        # ─── Demand zone (green legout) ──────────────────────────
        if lo_grn and SCAN_DEMAND:
            prox = b_hb
            dist = min(b_lw, leg_l, lo_l) if legin_red else min(b_lw, lo_l)
            confirm_close = C[start_bar - 1] if start_bar >= 1 else lo_c
            zone_valid = lo_c > b_hb or confirm_close > b_hb
            if not zone_valid:
                continue

            # Walk forward from legout-1 to current; check breach + count tests
            valid, tc, was_in = True, 0, False
            for v in range(start_bar - 1, -1, -1):
                v_low = L[v]
                if v_low < dist:
                    valid = False
                    break
                in_zone = v_low <= prox
                if in_zone and not was_in:
                    tc += 1
                was_in = in_zone
            if not valid or tc > MAX_ZONE_TESTS:
                continue

            is_gap  = lo_o > b_hb
            s_score = 2 if is_gap else (2 if nx_bpct >= EXCITE_PCT else 1)
            t_score = 2 if base_cnt <= 3 else (1 if base_cnt <= 5 else 0)
            f_score = 3.0 if tc == 0 else (1.5 if tc == 1 else 0.0)
            score = f_score + s_score + t_score

            if close_now > prox:
                dist_pct = (close_now - prox) / close_now * 100.0
                if best_dem is None or dist_pct < best_dem["dist_pct"]:
                    best_dem = {
                        "type":      "demand",
                        "proximal":  float(prox),
                        "distal":    float(dist),
                        "score":     float(score),
                        "tests":     int(tc),
                        "dist_pct":  float(dist_pct),
                    }

        # ─── Supply zone (red legout) ────────────────────────────
        if lo_red and SCAN_SUPPLY:
            prox = b_lb
            dist = max(b_hw, leg_h, lo_h) if legin_grn else max(b_hw, lo_h)
            confirm_close = C[start_bar - 1] if start_bar >= 1 else lo_c
            zone_valid = lo_c < b_lb or confirm_close < b_lb
            if not zone_valid:
                continue

            valid, tc, was_in = True, 0, False
            for v in range(start_bar - 1, -1, -1):
                v_high = H[v]
                if v_high > dist:
                    valid = False
                    break
                in_zone = v_high >= prox
                if in_zone and not was_in:
                    tc += 1
                was_in = in_zone
            if not valid or tc > MAX_ZONE_TESTS:
                continue

            is_gap  = lo_o < b_lb
            s_score = 2 if is_gap else (2 if nx_bpct >= EXCITE_PCT else 1)
            t_score = 2 if base_cnt <= 3 else (1 if base_cnt <= 5 else 0)
            f_score = 3.0 if tc == 0 else (1.5 if tc == 1 else 0.0)
            score = f_score + s_score + t_score

            if close_now < prox:
                dist_pct = (prox - close_now) / close_now * 100.0
                if best_sup is None or dist_pct < best_sup["dist_pct"]:
                    best_sup = {
                        "type":      "supply",
                        "proximal":  float(prox),
                        "distal":    float(dist),
                        "score":     float(score),
                        "tests":     int(tc),
                        "dist_pct":  float(dist_pct),
                    }

    return {"demand": best_dem, "supply": best_sup}


# ─── ALERT LOGIC ────────────────────────────────────────────────────────
def zone_key(symbol: str, zone: dict) -> str:
    """Stable dedupe key. Same prox/dist on same symbol = same zone."""
    return f"{symbol}_{zone['type']}_{round(zone['proximal'], 2)}_{round(zone['distal'], 2)}"


def is_approaching(close_now: float, zone: dict) -> bool:
    """True if price has crossed the entry line and is heading into the zone."""
    if zone["type"] == "demand":
        entry = zone["proximal"] * (1 + ALERT_ENTRY_PCT / 100.0)
        return close_now <= entry and close_now > zone["distal"]
    else:
        entry = zone["proximal"] * (1 - ALERT_ENTRY_PCT / 100.0)
        return close_now >= entry and close_now < zone["distal"]


def build_alert_msg(symbol: str, zone: dict, close_now: float, timeframe: str) -> str:
    if zone["type"] == "demand":
        entry = zone["proximal"] * (1 + ALERT_ENTRY_PCT / 100.0)
        direction = "🟢 *DEMAND* zone approach (↓)"
    else:
        entry = zone["proximal"] * (1 - ALERT_ENTRY_PCT / 100.0)
        direction = "🔴 *SUPPLY* zone approach (↑)"
    tf_label = "Daily" if timeframe == "1d" else "Weekly" if timeframe == "1wk" else timeframe
    return (
        f"{direction}\n"
        f"*{symbol}*  CMP `{close_now:.2f}`\n"
        f"Proximal: `{zone['proximal']:.2f}`\n"
        f"Distal:   `{zone['distal']:.2f}`\n"
        f"Entry line: `{entry:.2f}`\n"
        f"Score: *{zone['score']:.1f}*  |  Tests: {zone['tests']}  |  TF: {tf_label}"
    )


# ─── MAIN ───────────────────────────────────────────────────────────────
def scan_one_tf(timeframe: str, symbols: list[str]) -> list[tuple[str, str]]:
    """Scan all symbols for one timeframe. Returns list of (sym, msg) alerts."""
    state_file = state_path(timeframe)
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    new_state: dict = {}
    alerts: list[tuple[str, str]] = []

    tf_label = "Daily" if timeframe == "1d" else "Weekly" if timeframe == "1wk" else timeframe
    print()
    print(f"━━━━━━ {tf_label} scan ({timeframe}) ━━━━━━")
    print(f"Prev state: {len(state)} entries")

    for i, sym in enumerate(symbols, 1):
        print(f"[{tf_label[:3]} {i:3}/{len(symbols)}] {sym:14}", end=" ", flush=True)
        df = fetch_ohlc(sym, timeframe)
        if df is None:
            print("skip")
            continue

        close_now = float(df["Close"].iloc[-1])
        zones = detect_zones(df)
        dem, sup = zones["demand"], zones["supply"]

        msg_bits = []
        for z in (dem, sup):
            if z is None:
                continue
            if z["score"] < ALERT_MIN_SCORE:
                continue
            if not is_approaching(close_now, z):
                continue
            key = zone_key(sym, z)
            if key in state:
                new_state[key] = state[key]
                msg_bits.append(f"{z['type'][0].upper()}=seen")
            else:
                alerts.append((sym, build_alert_msg(sym, z, close_now, timeframe)))
                new_state[key] = {
                    "first_alerted": datetime.utcnow().isoformat(),
                    "score":         z["score"],
                    "cmp_at_alert":  close_now,
                }
                msg_bits.append(f"{z['type'][0].upper()}=NEW🔔")

        if not msg_bits:
            d_txt = f"D@{dem['dist_pct']:.1f}%/{dem['score']:.1f}" if dem else "D-"
            s_txt = f"S@{sup['dist_pct']:.1f}%/{sup['score']:.1f}" if sup else "S-"
            print(f"{d_txt} {s_txt}")
        else:
            print(" ".join(msg_bits))

        time.sleep(0.1)

    state_file.write_text(json.dumps(new_state, indent=2, sort_keys=True))
    print(f"{tf_label}: state now {len(new_state)} active zones")
    return alerts


def main() -> int:
    symbols = ALL_SYMBOLS
    print(f"━━━ Scanner ━━━")
    print(f"Symbols:    {len(symbols)}")
    print(f"Timeframes: {', '.join(TIMEFRAMES)}")
    print(f"Entry pct:  {ALERT_ENTRY_PCT}%")
    print(f"Min score:  {ALERT_MIN_SCORE}")

    all_alerts: list[tuple[str, str]] = []
    for tf in TIMEFRAMES:
        all_alerts.extend(scan_one_tf(tf, symbols))

    print()
    print(f"━━━ Alerts to send: {len(all_alerts)} ━━━")
    for sym, msg in all_alerts:
        print(f"  → {sym}")
        send_telegram(msg)
        time.sleep(0.5)   # avoid TG rate limit

    return 0


if __name__ == "__main__":
    sys.exit(main())
