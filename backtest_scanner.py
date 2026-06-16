"""Full-stack scanner backtest — replaces stocks/timeframes by editing CONFIG.

Runs the same alert pipeline that loop_scan.scan_iteration uses, against
historical snapshots, then walks forward to measure outcomes (WIN / LOSS /
UNTESTED / LIVE).

Cross-tabulates outcomes by every alert filter so you can see how much each
feature contributes to win rate:
   - Score (>= ALERT_MIN_SCORE)
   - Strict HTF filter (trend + closeness)
   - EMA20 confluence count (0 / 1 / 2 / 3 / 4)
   - Legout volume strength (STRONG / NORMAL / WEAK)
   - Swing origin from HTF (yes / no)

Usage:
    python backtest_scanner.py
    python backtest_scanner.py RELIANCE
    python backtest_scanner.py RELIANCE TCS INFY ABB

The CLI args override the SYMBOLS list in CONFIG.
"""
from __future__ import annotations

import sys
sys.stdout.reconfigure(encoding="utf-8")
from collections import defaultdict

from scanner import (
    detect_zones, compute_trend, compute_ema20, emas_in_zone, EMA20_TFS,
    find_swing_origin, find_origin_htf_match,
    is_approaching, passes_strict_filter, passes_125m_strict_filter,
    calc_trade_levels, fetch_ohlc,
    trend_tf_for, zone_tf_for,
)
# fetch_ohlc now applies auto_adjust_missed_corp_actions internally —
# yfinance with auto_adjust=False + that fix matches Dhan to 0.00% across
# diverse stocks (validated on 9 symbols). No Dhan dependency needed for
# backtest historical fetch.
from symbols import ALL_SYMBOLS


# Local copy of calc_trade_levels that takes buffers as parameters so we can
# run multiple buffer scenarios in a single backtest without re-importing.
def calc_trade_levels_with_buffers(zone: dict, entry_buf: float,
                                    sl_buf: float, rr: float) -> dict:
    prox = float(zone["proximal"])
    dist = float(zone["distal"])
    if zone["type"] == "demand":
        entry  = prox * (1.0 + entry_buf / 100.0)
        sl     = dist * (1.0 - sl_buf    / 100.0)
        risk   = entry - sl
        target = entry + rr * risk
    else:  # supply
        entry  = prox * (1.0 - entry_buf / 100.0)
        sl     = dist * (1.0 + sl_buf    / 100.0)
        risk   = sl - entry
        target = entry - rr * risk
    return {
        "entry":  float(entry),
        "sl":     float(sl),
        "target": float(target),
        "risk":   float(risk),
    }


# Each scenario is run on the SAME detected zones, so we can compare apples-to-
# apples how buffer size affects fill rate and win rate.
BUFFER_SCENARIOS = [
    {"name": "0.3%/2.6R", "entry_buf": 0.3, "sl_buf": 0.3, "rr": 2.6},
    {"name": "0.3%/2.0R", "entry_buf": 0.3, "sl_buf": 0.3, "rr": 2.0},
    {"name": "0.5%/2.6R", "entry_buf": 0.5, "sl_buf": 0.5, "rr": 2.6},
]

# ═══════════════════════════════════════════════════════════════════════
# CONFIG — edit these to change the backtest scope
# ═══════════════════════════════════════════════════════════════════════
# Default: the full production universe (Nifty 500 + momentum additions ≈ 528 stocks)
# Override via CLI args: python backtest_scanner.py SYMBOL1 SYMBOL2 ...
SYMBOLS = ALL_SYMBOLS

LTF_TIMEFRAMES = ["1d", "1wk"]    # which LTFs to backtest

# How many historical snapshots per (stock, tf) to test.
# With INTERVAL=1, every bar becomes a snapshot — true bar-by-bar walk-forward
# that mimics live trading. SNAPSHOTS_PER_STOCK caps how far back we go.
# 750 covers ~3y of daily history (most recent, most relevant). Weekly is
# capped naturally by available weekly bars (~260 bars over 5y).
SNAPSHOTS_PER_STOCK    = 750
SNAPSHOT_INTERVAL_D    = 1         # bar-by-bar daily
SNAPSHOT_INTERVAL_W    = 1         # bar-by-bar weekly

# Forward walk window per timeframe (how many bars to look ahead for outcome)
FORWARD_BARS = {"1d": 120, "1wk": 26, "125m": 60}

# Alert thresholds (mirror loop_scan defaults)
ALERT_MIN_SCORE       = 7.0
APPLY_IS_APPROACHING  = True       # require CMP to be within entry distance
APPLY_STRICT_FILTER   = True       # require HTF trend + closeness

# Output verbosity
SHOW_PER_ZONE_DETAIL  = False      # print every detected zone (very chatty)


# ═══════════════════════════════════════════════════════════════════════
# Outcome resolver
# ═══════════════════════════════════════════════════════════════════════
def walk_forward(df, cutoff: int, zone: dict, max_forward: int,
                 levels: dict | None = None) -> str:
    """Walk forward from cutoff. Returns 'WIN' / 'LOSS' / 'LIVE' / 'UNTESTED'.

    Starts AT cutoff (the alert bar) because the touch trigger fires intra-bar.
    The trade can fill AND hit SL/target within the same bar that fired the
    alert — skipping the alert bar would lose those same-day resolutions.
    Same-bar handling stays conservative: SL is checked before target, so
    if both happen in one bar we record LOSS (the worst case for the trader).

    `levels` lets the caller supply pre-computed entry/sl/target so we can
    test multiple buffer scenarios on the same zone. Falls back to the
    scanner's default calc_trade_levels when omitted.
    """
    tl = levels if levels is not None else calc_trade_levels(zone)
    entry, sl, target = tl["entry"], tl["sl"], tl["target"]
    end_idx = min(cutoff + 1 + max_forward, len(df))
    tested = False
    if zone["type"] == "demand":
        for i in range(cutoff, end_idx):
            low  = float(df["Low"].iloc[i])
            high = float(df["High"].iloc[i])
            if not tested and low <= entry:
                tested = True
                if low <= sl:
                    return "LOSS"
            if tested:
                if low <= sl:
                    return "LOSS"
                if high >= target:
                    return "WIN"
        return "LIVE" if tested else "UNTESTED"
    else:  # supply
        for i in range(cutoff, end_idx):
            low  = float(df["Low"].iloc[i])
            high = float(df["High"].iloc[i])
            if not tested and high >= entry:
                tested = True
                if high >= sl:
                    return "LOSS"
            if tested:
                if high >= sl:
                    return "LOSS"
                if low <= target:
                    return "WIN"
        return "LIVE" if tested else "UNTESTED"


# ═══════════════════════════════════════════════════════════════════════
# Per-snapshot pipeline mirror
# ═══════════════════════════════════════════════════════════════════════
def truncate_to_date(df, cutoff_date):
    """Truncate a df to bars whose index is <= cutoff_date."""
    if df is None:
        return None
    mask = df.index <= cutoff_date
    out = df[mask]
    return out if len(out) >= 20 else None


def evaluate_snapshot(sym: str, tf: str, cutoff: int,
                     df_ltf, dfs_htf: dict) -> list[dict]:
    """Run the FULL alert pipeline on a historical snapshot.

    Returns 0+ records, one per zone that would have alerted (or come close).
    Each record has all the filter signals plus a placeholder for outcome.
    """
    records = []
    df_snap = df_ltf.iloc[:cutoff + 1]
    if len(df_snap) < 50:
        return records

    cutoff_date = df_snap.index[-1]

    # Truncate HTF dfs to the same point in time
    htf_snaps = {}
    for htf_tf, df_h in dfs_htf.items():
        htf_snaps[htf_tf] = truncate_to_date(df_h, cutoff_date)

    # close_now = last closed bar's close (mirrors IGNORE_INPROGRESS_BAR=True)
    close_now = float(df_snap["Close"].iloc[-2]) if len(df_snap) > 1 \
                else float(df_snap["Close"].iloc[-1])

    # LTF zones
    # Pass entry_pct so LTF zones get the departure-then-return gate
    from scanner import entry_pct_for
    zones = detect_zones(df_snap, close_now_override=close_now,
                          use_close_beyond_legin=True,
                          entry_pct=entry_pct_for(tf))

    ltf_trend = compute_trend(df_snap)
    trend_htf_tf = trend_tf_for(tf)
    zone_htf_tf  = zone_tf_for(tf)
    df_trend_htf = htf_snaps.get(trend_htf_tf)
    df_zone_htf  = htf_snaps.get(zone_htf_tf)
    trend_htf = compute_trend(df_trend_htf) if df_trend_htf is not None else 0
    htf_z = detect_zones(df_zone_htf, close_now_override=close_now) \
            if df_zone_htf is not None else {"demand": None, "supply": None}

    # EMA20 across D/W/M/3M
    ema20s = {}
    for ema_tf in EMA20_TFS:
        df_ema = htf_snaps.get(ema_tf) if ema_tf != tf else df_snap
        if df_ema is None and ema_tf == tf:
            df_ema = df_snap
        ema20s[ema_tf] = compute_ema20(df_ema) if df_ema is not None else None

    # Swing origin
    origin_price = None
    origin_match = None
    for z in (zones["demand"], zones["supply"]):
        if z is None:
            continue
        if origin_price is None:
            origin_price = find_swing_origin(df_snap, z["type"])

    for side in ("demand", "supply"):
        z = zones.get(side)
        if z is None:
            continue

        # === Pipeline filters (record pass/fail for each) ===
        passes_score      = z["score"] >= ALERT_MIN_SCORE
        # Touch-based trigger: alert fires when the CURRENT bar's intra-bar
        # range actually touches the proximal line.
        #   demand: bar's Low  ≤ proximal  (price falls into zone)
        #   supply: bar's High ≥ proximal  (price rallies into zone)
        # Stricter than the live "within entry_pct%" approach gate, which is
        # designed for early warning. Backtest measures fills, not warnings.
        this_bar = df_ltf.iloc[cutoff]
        if APPLY_IS_APPROACHING:
            if side == "demand":
                passes_approach = float(this_bar["Low"])  <= z["proximal"]
            else:
                passes_approach = float(this_bar["High"]) >= z["proximal"]
        else:
            passes_approach = True

        if APPLY_STRICT_FILTER:
            if tf == "125m":
                passes_strict = passes_125m_strict_filter(
                    z["type"], z, trend_htf,
                    htf_z["demand"], htf_z["supply"],
                    w_score_threshold=ALERT_MIN_SCORE,
                )
            else:
                passes_strict = passes_strict_filter(
                    z["type"], trend_htf, htf_z["demand"], htf_z["supply"],
                )
        else:
            passes_strict = True

        # Annotations (always computed for analysis)
        ema_count, _ = emas_in_zone(z, ema20s)
        vol_label = z.get("vol_label") or "UNKNOWN"
        vol_ratio = z.get("vol_ratio")

        # Swing-origin match: would need HTF zones across W/M/3M.
        # Reuse the htf_snaps we already have.
        origin_match_for_z = None
        if origin_price is not None:
            origin_zones_pkg = {
                htf_tf_: detect_zones(htf_snaps.get(htf_tf_),
                                       close_now_override=close_now)
                          if htf_snaps.get(htf_tf_) is not None
                          else {"demand": None, "supply": None}
                for htf_tf_ in ("1wk", "1mo", "3mo")
            }
            origin_match_for_z = find_origin_htf_match(
                origin_price, z["type"], origin_zones_pkg, ltf_timeframe=tf,
            )

        records.append({
            "sym":            sym,
            "tf":             tf,
            "cutoff":         cutoff,
            "date":           str(cutoff_date.date()) if hasattr(cutoff_date, "date") else str(cutoff_date),
            "side":           side,
            "score":          z["score"],
            "tests":          z["tests"],
            "vol_label":      vol_label,
            "vol_ratio":      vol_ratio,
            "ema_count":      ema_count,
            "origin_hit":     origin_match_for_z is not None,
            "ltf_trend":      ltf_trend,
            "htf_trend":      trend_htf,
            "passes_score":   passes_score,
            "passes_approach":passes_approach,
            "passes_strict":  passes_strict,
            "zone":           z,
            "outcome":        None,    # filled by walk_forward
        })

    return records


# ═══════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════
def crosstab(rows: list[dict], group_key: str, title: str):
    """Print a single-dimension cross-tab of WIN/LOSS counts."""
    by_g = defaultdict(lambda: {"WIN": 0, "LOSS": 0, "LIVE": 0, "UNTESTED": 0})
    for r in rows:
        if r["outcome"] is None: continue
        by_g[r[group_key]][r["outcome"]] += 1
    print(f"\n--- {title} ---")
    print(f"{group_key.upper():16} {'Total':>6} {'WIN':>5} {'LOSS':>5} {'LIVE':>5} {'UNTESTED':>9}  WinRate")
    for g, d in sorted(by_g.items(), key=lambda x: str(x[0])):
        total = sum(d.values())
        wins, losses = d["WIN"], d["LOSS"]
        resolved = wins + losses
        wr = (wins / resolved * 100) if resolved else 0.0
        print(f"{str(g):16} {total:6d} {wins:5d} {losses:5d} {d['LIVE']:5d} {d['UNTESTED']:9d}     {wr:5.1f}%")


def scenario_summary(rows: list[dict], title: str):
    """Top-line summary of WIN/LOSS/LIVE/UNTESTED for EVERY scenario."""
    print(f"\n--- {title} ---")
    print(f"{'Scenario':>10} {'Total':>6} {'WIN':>5} {'LOSS':>5} {'LIVE':>5} {'UNTESTED':>9}  WinRate  FillRate")
    for scen in BUFFER_SCENARIOS:
        n = scen["name"]
        counts = {"WIN": 0, "LOSS": 0, "LIVE": 0, "UNTESTED": 0}
        for r in rows:
            o = r.get("outcomes", {}).get(n)
            if o in counts: counts[o] += 1
        total    = sum(counts.values())
        resolved = counts["WIN"] + counts["LOSS"]
        filled   = resolved + counts["LIVE"]
        wr = (counts["WIN"] / resolved * 100) if resolved else 0.0
        fr = (filled / total * 100) if total else 0.0
        print(f"{n:>10} {total:6d} {counts['WIN']:5d} {counts['LOSS']:5d} "
              f"{counts['LIVE']:5d} {counts['UNTESTED']:9d}    {wr:5.1f}%   {fr:5.1f}%")


def crosstab_scenario(rows: list[dict], scen_name: str, group_key: str, title: str):
    """Single-dimension crosstab using a specific scenario's outcomes."""
    by_g = defaultdict(lambda: {"WIN": 0, "LOSS": 0, "LIVE": 0, "UNTESTED": 0})
    for r in rows:
        o = r.get("outcomes", {}).get(scen_name)
        if o is None: continue
        by_g[r[group_key]][o] += 1
    print(f"\n--- {title} ---")
    print(f"{group_key.upper():16} {'Total':>6} {'WIN':>5} {'LOSS':>5} {'LIVE':>5} {'UNTESTED':>9}  WinRate")
    for g, d in sorted(by_g.items(), key=lambda x: str(x[0])):
        total = sum(d.values())
        wins, losses = d["WIN"], d["LOSS"]
        resolved = wins + losses
        wr = (wins / resolved * 100) if resolved else 0.0
        print(f"{str(g):16} {total:6d} {wins:5d} {losses:5d} {d['LIVE']:5d} {d['UNTESTED']:9d}     {wr:5.1f}%")


def double_crosstab(rows: list[dict], k1: str, k2: str, title: str):
    """Two-dimensional cross-tab."""
    print(f"\n--- {title} ---")
    grid = defaultdict(lambda: defaultdict(lambda: {"WIN": 0, "LOSS": 0}))
    for r in rows:
        if r["outcome"] not in ("WIN", "LOSS"): continue
        grid[r[k1]][r[k2]][r["outcome"]] += 1
    vals_k2 = sorted({r[k2] for r in rows if r["outcome"] in ("WIN", "LOSS")},
                     key=str)
    header = f"{k1.upper():16} " + " ".join(f"{str(v):>14}" for v in vals_k2)
    print(header)
    for k1_val in sorted(grid.keys(), key=str):
        row = grid[k1_val]
        cells = []
        for v in vals_k2:
            d = row[v]
            w, l = d["WIN"], d["LOSS"]
            r_ = w + l
            wr = (w / r_ * 100) if r_ else 0.0
            cells.append(f"{w:2d}W/{l:2d}L ({wr:4.1f}%)")
        print(f"{str(k1_val):16} " + " ".join(f"{c:>14}" for c in cells))


def main(symbols: list[str]):
    print("=" * 84)
    print("Scanner backtest — full alert pipeline through history")
    print("=" * 84)
    print(f"Symbols:               {len(symbols)}  ({', '.join(symbols)})")
    print(f"Timeframes:            {LTF_TIMEFRAMES}")
    print(f"Snapshots per stock:   {SNAPSHOTS_PER_STOCK}")
    print(f"ALERT_MIN_SCORE:       {ALERT_MIN_SCORE}")
    print(f"APPLY_STRICT_FILTER:   {APPLY_STRICT_FILTER}")
    print(f"APPLY_IS_APPROACHING:  {APPLY_IS_APPROACHING}")

    # Three dedup tracks. Each captures a zone ONCE at the relevant moment:
    #
    #   DETECTED:   first time detect_zones() returns the zone.
    #               Price may be very far from it. Filter validation baseline.
    #   APPROACHED: first time price is within ALERT_ENTRY_PCT of the zone.
    #               The "trader would have considered this" moment.
    #   ALERTED:    first time ALL alert filters pass (score + approach + strict).
    #               Matches production state-file dedup — what the live scanner
    #               would actually message you about.
    seen_detected:   set[tuple] = set()
    seen_approached: set[tuple] = set()
    seen_alerted:    set[tuple] = set()

    all_detected:   list[dict] = []
    all_approached: list[dict] = []
    all_alerted:    list[dict] = []

    def zone_dedup_key(sym, tf, z) -> tuple:
        return (sym, tf, z["type"], round(z["proximal"], 2), round(z["distal"], 2))

    def record_with_outcomes(r_in: dict, cutoff: int, df_ltf, forward: int) -> dict:
        """Snapshot a record's state and walk forward under every buffer scenario."""
        r_out = dict(r_in)
        r_out["outcomes"] = {}
        r_out["levels"]   = {}
        for scen in BUFFER_SCENARIOS:
            lvls = calc_trade_levels_with_buffers(
                r_in["zone"], scen["entry_buf"], scen["sl_buf"], scen["rr"],
            )
            r_out["outcomes"][scen["name"]] = walk_forward(
                df_ltf, cutoff, r_in["zone"], forward, levels=lvls,
            )
            r_out["levels"][scen["name"]] = lvls
        r_out["outcome"] = r_out["outcomes"][BUFFER_SCENARIOS[0]["name"]]
        return r_out

    # Order timeframes by how much history each needs (longest first).
    _PERIOD_RANK = {"3mo": 4, "1mo": 3, "1wk": 2, "1d": 1, "125m": 0, "5m": 0}

    # Override default 1d fetch (which is only 1y) with a 5y fetch so the
    # day-by-day walk has full 5-year history to backtest over. yfinance
    # supports 5y daily natively; auto-fix is applied via fetch_ohlc.
    import yfinance as _yf
    from scanner import _drop_phantom_bars as _dpb
    from scanner import auto_adjust_missed_corp_actions as _autofix

    def _fetch_5y_daily(sym):
        df = _yf.download(sym + ".NS", period="5y", interval="1d",
                          progress=False, auto_adjust=False, actions=False, threads=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df = _dpb(df)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return _autofix(df) if len(df) >= 20 else None

    for sym in symbols:
        all_tfs = set(LTF_TIMEFRAMES) | set(EMA20_TFS) | {"1wk", "1mo", "3mo"}
        ordered_tfs = sorted(all_tfs, key=lambda tf: -_PERIOD_RANK.get(tf, 0))
        dfs = {}
        for tf in ordered_tfs:
            if tf == "1d":
                dfs[tf] = _fetch_5y_daily(sym)
            else:
                dfs[tf] = fetch_ohlc(sym, tf)
        for tf in LTF_TIMEFRAMES:
            df_ltf = dfs[tf]
            if df_ltf is None or len(df_ltf) < 100:
                print(f"  {sym:12} {tf:4}  skip (insufficient data)")
                continue
            interval = SNAPSHOT_INTERVAL_D if tf == "1d" else SNAPSHOT_INTERVAL_W
            forward = FORWARD_BARS.get(tf, 60)
            n = len(df_ltf)
            n_d, n_app, n_alt = 0, 0, 0
            # Iterate OLDEST snapshot first so the FIRST-MEETING-CRITERIA instance
            # of each zone is the one we record. This matches production where
            # the FIRST time all filters pass simultaneously is when the alert
            # fires; subsequent passes are dedup'd by the state file.
            for snap_idx in range(SNAPSHOTS_PER_STOCK - 1, -1, -1):
                cutoff = n - 1 - forward - interval * snap_idx
                if cutoff < 50: continue
                records = evaluate_snapshot(sym, tf, cutoff, df_ltf, dfs)
                for r in records:
                    key = zone_dedup_key(sym, tf, r["zone"])
                    is_app = r["passes_approach"]
                    is_alt = is_app and r["passes_score"] and r["passes_strict"]

                    if key not in seen_detected:
                        seen_detected.add(key)
                        all_detected.append(record_with_outcomes(r, cutoff, df_ltf, forward))
                        n_d += 1
                    if is_app and key not in seen_approached:
                        seen_approached.add(key)
                        all_approached.append(record_with_outcomes(r, cutoff, df_ltf, forward))
                        n_app += 1
                    if is_alt and key not in seen_alerted:
                        seen_alerted.add(key)
                        all_alerted.append(record_with_outcomes(r, cutoff, df_ltf, forward))
                        n_alt += 1
            print(f"  {sym:12} {tf:4}  detected:{n_d:3d}  approached:{n_app:3d}  alerted:{n_alt:3d}")

    if not all_detected:
        print("No zones detected.")
        return

    print()
    print("=" * 84)
    print(f"DETECTED: {len(all_detected)}  |  APPROACHED: {len(all_approached)}  "
          f"|  ALERTED (production-equivalent): {len(all_alerted)}")
    print("=" * 84)

    # Backwards-compat aliases for the existing report functions
    all_rows = all_detected
    alerted  = all_alerted

    # === Head-to-head: buffer scenarios compared on the SAME zones ===
    print("\n" + "═" * 84)
    print("BUFFER-SCENARIO COMPARISON  (same zones, different Entry/SL buffers)")
    print("═" * 84)
    scenario_summary(all_detected,   f"DETECTED   — {len(all_detected):4d} zones (first detection)")
    scenario_summary(all_approached, f"APPROACHED — {len(all_approached):4d} zones (first approach: trader-visible moment)")
    if all_alerted:
        scenario_summary(all_alerted, f"ALERTED    — {len(all_alerted):4d} zones (first alert: production-equivalent)")

    # ─── Reports on the FULL set (every zone detected, regardless of filter) ───
    print("\n" + "═" * 84)
    print("FULL SET — every detected zone, regardless of alert filters")
    print("═" * 84)
    crosstab(all_rows, "vol_label", "By Volume verdict")
    crosstab(all_rows, "ema_count", "By EMA20 count in zone")
    crosstab(all_rows, "passes_strict", "By Strict filter pass")
    crosstab(all_rows, "passes_score", "By Score >= threshold")
    crosstab(all_rows, "origin_hit", "By HTF origin found")
    crosstab(all_rows, "tf", "By Timeframe")

    # ─── Reports on the ALERTED set (real alerts) ───
    if alerted:
        print("\n" + "═" * 84)
        print(f"ALERTED SET — {len(alerted)} zones that passed all alert filters")
        print("═" * 84)
        crosstab(alerted, "vol_label", "Alerted — By Volume verdict")
        crosstab(alerted, "ema_count", "Alerted — By EMA20 count in zone")
        crosstab(alerted, "origin_hit", "Alerted — By HTF origin found")
        crosstab(alerted, "tf",        "Alerted — By Timeframe")
        crosstab(alerted, "sym",       "Alerted — By Symbol")

        double_crosstab(alerted, "vol_label", "ema_count",
                         "Alerted — Volume × EMA20 count")
        double_crosstab(alerted, "vol_label", "origin_hit",
                         "Alerted — Volume × HTF origin")
        double_crosstab(alerted, "ema_count", "origin_hit",
                         "Alerted — EMA20 × HTF origin")

    if SHOW_PER_ZONE_DETAIL:
        print("\n" + "═" * 84)
        print("Every alerted zone (date / sym / details):")
        print("═" * 84)
        for r in alerted:
            print(f"  {r['date']} {r['sym']:12} {r['tf']:4} {r['side']:6} "
                  f"score={r['score']:.1f} vol={r['vol_label']} "
                  f"ema={r['ema_count']} origin={'✓' if r['origin_hit'] else '✗'} "
                  f"→ {r['outcome']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli_syms = [a.upper() for a in sys.argv[1:]]
        print(f"(Using CLI symbols: {cli_syms})")
        main(cli_syms)
    else:
        main(SYMBOLS)
