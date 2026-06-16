"""Detailed VEDL audit — print every alerted zone with levels and resolution.

For each alerted zone:
  * Alert date (when production would have sent the Telegram)
  * Zone proximal / distal / type
  * Score / tests / vol / EMA count / origin
  * Entry / SL / Target (at 0.3% buffer, R:R 2.6)
  * Outcome: WIN / LOSS / UNTESTED / LIVE
  * Resolution date (the bar that hit target or SL)
  * If LOSS — which bar's low/high broke the SL

Run:
    python _vedl_audit.py            # both daily and weekly
    python _vedl_audit.py 1d         # daily only
    python _vedl_audit.py 1wk        # weekly only
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

from scanner import (
    detect_zones, compute_trend, compute_ema20, emas_in_zone, EMA20_TFS,
    find_swing_origin, find_origin_htf_match,
    is_approaching, passes_strict_filter, passes_125m_strict_filter,
    calc_trade_levels, entry_pct_for,
    trend_tf_for, zone_tf_for,
)
import yfinance as yf
from scanner import fetch_ohlc, _drop_phantom_bars, auto_adjust_missed_corp_actions


# Override 1d period to fetch 5y instead of fetch_ohlc's default 1y
DAILY_YEARS_FOR_AUDIT = 5


def _fetch_daily_extended(sym: str) -> "pd.DataFrame | None":
    """Fetch 5y of daily directly via yfinance, bypassing fetch_ohlc's 1y limit.
    Applies the same auto_adjust_missed_corp_actions fix as fetch_ohlc."""
    df = yf.download(sym + ".NS", period=f"{DAILY_YEARS_FOR_AUDIT}y", interval="1d",
                     progress=False, auto_adjust=False, actions=False, threads=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = _drop_phantom_bars(df)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    if len(df) < 20:
        return None
    return auto_adjust_missed_corp_actions(df)

# Match backtest_scanner.py forward windows
FORWARD_BARS = {"1d": 120, "1wk": 26, "125m": 60}


def walk_forward_with_dates(df, cutoff, zone, max_forward, levels):
    """Same as backtest_scanner.walk_forward but ALSO returns resolution info.

    Starts AT cutoff (not cutoff+1) to capture same-bar resolution — trades
    that fill and hit SL/target within the alert bar itself. Mirrors the
    same-bar fix in backtest_scanner.walk_forward.
    """
    entry, sl, target = levels["entry"], levels["sl"], levels["target"]
    end_idx = min(cutoff + 1 + max_forward, len(df))
    tested = False
    fill_date = None

    if zone["type"] == "demand":
        for i in range(cutoff, end_idx):
            low  = float(df["Low"].iloc[i])
            high = float(df["High"].iloc[i])
            date = df.index[i]
            if not tested and low <= entry:
                tested = True
                fill_date = date
                if low <= sl:
                    return ("LOSS", date, fill_date, "same-bar SL puncture")
            if tested:
                if low <= sl:
                    return ("LOSS", date, fill_date, f"low={low:.2f} ≤ SL={sl:.2f}")
                if high >= target:
                    return ("WIN", date, fill_date, f"high={high:.2f} ≥ T={target:.2f}")
        return ("LIVE" if tested else "UNTESTED", None, fill_date, "")
    else:  # supply
        for i in range(cutoff, end_idx):
            low  = float(df["Low"].iloc[i])
            high = float(df["High"].iloc[i])
            date = df.index[i]
            if not tested and high >= entry:
                tested = True
                fill_date = date
                if high >= sl:
                    return ("LOSS", date, fill_date, "same-bar SL puncture")
            if tested:
                if high >= sl:
                    return ("LOSS", date, fill_date, f"high={high:.2f} ≥ SL={sl:.2f}")
                if low <= target:
                    return ("WIN", date, fill_date, f"low={low:.2f} ≤ T={target:.2f}")
        return ("LIVE" if tested else "UNTESTED", None, fill_date, "")


def truncate_to_date(df, cutoff_date):
    if df is None:
        return None
    out = df[df.index <= cutoff_date]
    return out if len(out) >= 20 else None


def audit_one_tf(sym: str, tf: str):
    print(f"\n{'='*100}")
    print(f"{sym} — {tf.upper()} ALERT AUDIT")
    print(f"{'='*100}")

    # Pre-fetch all needed dfs (same set as backtest_scanner.evaluate_snapshot)
    needed_tfs = {tf} | set(EMA20_TFS) | {"1wk", "1mo", "3mo"}
    print(f"Fetching {sym} for timeframes: {sorted(needed_tfs)}  "
          f"(daily extended to {DAILY_YEARS_FOR_AUDIT}y)")
    dfs = {}
    for t in sorted(needed_tfs, key=lambda x: -{"3mo": 4, "1mo": 3, "1wk": 2, "1d": 1}.get(x, 0)):
        if t == "1d":
            # Override fetch_ohlc's 1y default — we need 5y for proper backtest
            dfs[t] = _fetch_daily_extended(sym)
        else:
            dfs[t] = fetch_ohlc(sym, t)

    df_ltf = dfs[tf]
    if df_ltf is None or len(df_ltf) < 100:
        print(f"Not enough bars for {tf} — skipping.")
        return

    forward = FORWARD_BARS.get(tf, 60)
    n = len(df_ltf)
    print(f"{tf} bars: {n}  range: {df_ltf.index[0].date()} → {df_ltf.index[-1].date()}")

    seen_alerted: set = set()    # (side, prox, distal) → dedup once-per-zone like prod state file

    # Walk bar-by-bar from oldest to newest
    alerts: list[dict] = []
    near_miss: list[dict] = []   # zones that approached but failed score or strict
    for cutoff in range(50, n - forward):
        df_snap = df_ltf.iloc[:cutoff + 1]
        if len(df_snap) < 50:
            continue
        cutoff_date = df_snap.index[-1]

        htf_snaps = {h: truncate_to_date(dfs.get(h), cutoff_date) for h in set(EMA20_TFS) | {"1wk", "1mo", "3mo"}}

        close_now = float(df_snap["Close"].iloc[-2]) if len(df_snap) > 1 \
                    else float(df_snap["Close"].iloc[-1])

        zones = detect_zones(df_snap, close_now_override=close_now,
                              use_close_beyond_legin=True,
                              entry_pct=entry_pct_for(tf))

        trend_htf_tf = trend_tf_for(tf)
        zone_htf_tf  = zone_tf_for(tf)
        trend_htf = compute_trend(htf_snaps.get(trend_htf_tf)) \
                    if htf_snaps.get(trend_htf_tf) is not None else 0
        htf_z = detect_zones(htf_snaps.get(zone_htf_tf), close_now_override=close_now) \
                if htf_snaps.get(zone_htf_tf) is not None \
                else {"demand": None, "supply": None}

        ema20s = {}
        for ema_tf in EMA20_TFS:
            df_ema = htf_snaps.get(ema_tf) if ema_tf != tf else df_snap
            ema20s[ema_tf] = compute_ema20(df_ema) if df_ema is not None else None

        for side in ("demand", "supply"):
            z = zones.get(side)
            if z is None:
                continue

            key = (side, round(z["proximal"], 2), round(z["distal"], 2))
            if key in seen_alerted:
                continue

            passes_score    = z["score"] >= 7.0
            # Touch-based trigger (backtest only): alert fires when the CURRENT
            # bar's intra-bar range actually touches the proximal line.
            #   demand: bar's Low  ≤ proximal  (price falls into zone)
            #   supply: bar's High ≥ proximal  (price rallies into zone)
            # Stricter than the live "within entry_pct%" approach gate.
            this_bar = df_ltf.iloc[cutoff]
            if side == "demand":
                passes_approach = float(this_bar["Low"])  <= z["proximal"]
            else:
                passes_approach = float(this_bar["High"]) >= z["proximal"]
            passes_strict   = passes_strict_filter(
                z["type"], trend_htf, htf_z["demand"], htf_z["supply"],
            )

            # Track zone failure reasons too — only continue for alerts though
            if not (passes_score and passes_approach and passes_strict):
                # Record near-miss zones (price approached) so we can see why
                # they didn't alert. Dedup the same way as alerts.
                if passes_approach:
                    reasons = []
                    if not passes_score:  reasons.append(f"score={z['score']:.1f}<7")
                    if not passes_strict: reasons.append("strict fail")
                    near_miss.append({
                        "date":     cutoff_date,
                        "side":     side,
                        "prox":     z["proximal"],
                        "distal":   z["distal"],
                        "score":    z["score"],
                        "tests":    z["tests"],
                        "reasons":  reasons,
                    })
                    seen_alerted.add(key)
                continue

            seen_alerted.add(key)

            ema_count, ema_hits = emas_in_zone(z, ema20s)

            origin_price = find_swing_origin(df_snap, z["type"])
            origin_match = None
            if origin_price is not None:
                origin_pkg = {
                    h: detect_zones(htf_snaps.get(h), close_now_override=close_now)
                       if htf_snaps.get(h) is not None
                       else {"demand": None, "supply": None}
                    for h in ("1wk", "1mo", "3mo")
                }
                origin_match = find_origin_htf_match(
                    origin_price, z["type"], origin_pkg, ltf_timeframe=tf,
                )

            lvls = calc_trade_levels(z)
            outcome, resolve_date, fill_date, why = walk_forward_with_dates(
                df_ltf, cutoff, z, forward, lvls,
            )

            alerts.append({
                "alert_date":   cutoff_date,
                "side":         side,
                "score":        z["score"],
                "tests":        z["tests"],
                "prox":         z["proximal"],
                "distal":       z["distal"],
                "vol_label":    z.get("vol_label") or "-",
                "vol_ratio":    z.get("vol_ratio"),
                "ema_count":    ema_count,
                "ema_hits":     ema_hits,
                "origin":       "✓" if origin_match else "✗",
                "entry":        lvls["entry"],
                "sl":           lvls["sl"],
                "target":       lvls["target"],
                "outcome":      outcome,
                "fill_date":    fill_date,
                "resolve_date": resolve_date,
                "why":          why,
            })

    if not alerts:
        print(f"\nNo alerts on {tf} for VEDL in the available history.")
        if near_miss:
            print(f"\nHowever, {len(near_miss)} zones came within entry distance "
                  f"but failed score/strict filters:\n")
            print(f"  {'Date':12s} {'Side':6s} {'Prox':>8s} {'Distal':>8s} "
                  f"{'Score':>5s} {'Tests':>5s}  Why")
            for nm in near_miss:
                date_str = str(nm["date"].date() if hasattr(nm["date"], "date") else nm["date"])
                print(f"  {date_str:12s} {nm['side']:6s} {nm['prox']:>8.2f} {nm['distal']:>8.2f} "
                      f"{nm['score']:>5.1f} {nm['tests']:>5d}  {', '.join(nm['reasons'])}")
        return

    print(f"\nTotal {tf} alerts: {len(alerts)}")
    # Wide table
    print()
    hdr = (f"{'#':>3} {'AlertDate':12s} {'Side':6s} {'Prox':>8s} {'Distal':>8s} "
           f"{'Score':>5s} {'Vol':>6s} {'EMA':>3s} {'Org':>3s} "
           f"{'Entry':>8s} {'SL':>8s} {'Target':>8s} "
           f"{'Result':>9s} {'FillDate':12s} {'ResolvedOn':12s}")
    print(hdr)
    print("-" * len(hdr))
    for i, a in enumerate(alerts, 1):
        date_str = str(a["alert_date"].date() if hasattr(a["alert_date"], "date") else a["alert_date"])
        fill_str = str(a["fill_date"].date() if a["fill_date"] is not None else "")
        rsv_str  = str(a["resolve_date"].date() if a["resolve_date"] is not None else "")
        print(f"{i:>3} {date_str:12s} {a['side']:6s} {a['prox']:>8.2f} {a['distal']:>8.2f} "
              f"{a['score']:>5.1f} {a['vol_label']:>6s} {a['ema_count']:>3d} {a['origin']:>3s} "
              f"{a['entry']:>8.2f} {a['sl']:>8.2f} {a['target']:>8.2f} "
              f"{a['outcome']:>9s} {fill_str:12s} {rsv_str:12s}")

    # Summary
    w  = sum(1 for a in alerts if a["outcome"] == "WIN")
    l  = sum(1 for a in alerts if a["outcome"] == "LOSS")
    lv = sum(1 for a in alerts if a["outcome"] == "LIVE")
    u  = sum(1 for a in alerts if a["outcome"] == "UNTESTED")
    resolved = w + l
    wr = (w / resolved * 100) if resolved else 0.0
    print()
    print(f"Summary: {w} WIN | {l} LOSS | {lv} LIVE | {u} UNTESTED | WR = {wr:.1f}% (resolved n={resolved})")


def main():
    # Usage: python _vedl_audit.py [SYMBOL] [TF ...]
    # Default: VEDL on 1d + 1wk
    args = sys.argv[1:]
    sym = args[0] if args else "VEDL"
    tfs = args[1:] or ["1d", "1wk"]
    for tf in tfs:
        audit_one_tf(sym, tf)


if __name__ == "__main__":
    main()
